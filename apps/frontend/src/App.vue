<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import CodeEditorBox from './Components/CodeEditorBox.vue'
import AiFeedbackpanel from './Components/AiFeedbackpanel.vue'
import AssignmentList from './Components/AssignmentList.vue'
import {
  clearAccessToken,
  getAccessToken,
  getAssignments,
  getSubmissionHistory,
  getSubmission,
  login,
  openSubmissionSocket,
  requestHint,
  register,
  runSource,
  setAccessToken,
  submitSource,
  type Assignment,
  type LlmBackend,
  type SubmissionAction,
  type SubmissionNotification,
  type SubmissionStatus,
} from './services/api'

const assignments = ref<Assignment[]>([])
const solvedByAssignmentId = ref<Record<string, boolean | null>>({})
const selectedId = ref('')
const selected = computed(() => assignments.value.find(assignment => assignment.id === selectedId.value))
const code = ref('#include <stdio.h>\n\nint main(void) {\n    return 0;\n}')
const feedback = ref('')
const busyAction = ref<'hint' | 'run' | 'submit' | null>(null)
const error = ref('')
const terminalOutput = ref('')
const progress = ref('')
const status = ref<SubmissionStatus | ''>('')
const backend = ref<LlmBackend>('azure')
const username = ref('')
const password = ref('')
const authMode = ref<'login' | 'register'>('login')
const authenticated = ref(Boolean(getAccessToken()))
const authLoading = ref(false)
const authError = ref('')
const submissionId = ref('')
const loading = computed(() => busyAction.value === 'submit' || busyAction.value === 'hint')
const waitingForAction = computed(() => busyAction.value !== null)
let socket: WebSocket | undefined
let reconnectTimer: number | undefined
let reconnectAttempts = 0
let pollGeneration = 0

function selectAssignment(id: string) {
  selectedId.value = id
  code.value = '#include <stdio.h>\n\nint main(void) {\n    return 0;\n}'
  feedback.value = ''
  error.value = ''
  terminalOutput.value = ''
}

async function loadAssignments() {
  const [assignmentRows, submissions] = await Promise.all([getAssignments(), getSubmissionHistory()])
  assignments.value = assignmentRows
  const latestSubmitByAssignment: Record<string, boolean | null> = {}
  for (const submission of submissions) {
    if (submission.action === 'submit' && !(submission.assignmentId in latestSubmitByAssignment)) {
      latestSubmitByAssignment[submission.assignmentId] = submission.taskSolved
    }
  }
  solvedByAssignmentId.value = latestSubmitByAssignment
  if (!assignments.value.some(assignment => assignment.id === selectedId.value)) {
    selectedId.value = assignments.value[0]?.id ?? ''
  }
}

async function authenticate() {
  authLoading.value = true
  authError.value = ''
  try {
    if (authMode.value === 'register') await register(username.value, password.value)
    const session = await login(username.value, password.value)
    setAccessToken(session.accessToken)
    authenticated.value = true
    await loadAssignments()
  } catch (cause) {
    authError.value = cause instanceof Error ? cause.message : 'Authentication failed.'
  } finally {
    authLoading.value = false
  }
}

function logout() {
  clearAccessToken()
  authenticated.value = false
  assignments.value = []
  submissionId.value = ''
  busyAction.value = null
  pollGeneration++
  window.clearTimeout(reconnectTimer)
  socket?.close()
}

function onProgress(notification: SubmissionNotification) {
  progress.value = notification.message
  if (notification.stage) status.value = notification.stage as SubmissionStatus
}

function connectSocket(id: string, generation: number) {
  socket = openSubmissionSocket(id, onProgress, () => {
    if (generation !== pollGeneration || !waitingForAction.value || reconnectAttempts >= 5) return
    const delay = Math.min(1000 * 2 ** reconnectAttempts, 10000)
    reconnectAttempts++
    progress.value = 'Reconnecting to live updates; status polling remains active'
    reconnectTimer = window.setTimeout(() => connectSocket(id, generation), delay)
  })
  socket.addEventListener('open', () => {
    reconnectAttempts = 0
  })
}

async function pollSubmission(id: string, generation: number) {
  while (generation === pollGeneration) {
    try {
      const result = await getSubmission(id)
      status.value = result.status
      if (result.status === 'COMPLETED') {
        if (result.action === 'submit') {
          solvedByAssignmentId.value[result.assignmentId] = result.taskSolved
        }
        feedback.value = result.action === 'run'
          ? ''
          : result.llmFeedback ?? JSON.stringify(result.sandboxOutput, null, 2)
        terminalOutput.value = formatTerminalOutput(result.sandboxOutput)
        progress.value = 'Completed'
        busyAction.value = null
        socket?.close()
        return
      }
      if (result.status === 'FAILED') {
        error.value = result.errorMessage ?? 'The submission failed.'
        terminalOutput.value = error.value
        if (result.action === 'submit') {
          solvedByAssignmentId.value[result.assignmentId] = result.taskSolved
        }
        progress.value = 'Failed'
        busyAction.value = null
        socket?.close()
        return
      }
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : 'Could not retrieve submission status.'
      busyAction.value = null
      return
    }

    await new Promise(resolve => window.setTimeout(resolve, 2000))
  }
}

function trackSubmission(accepted: { submissionId: string; status: SubmissionStatus }, action: SubmissionAction) {
  submissionId.value = accepted.submissionId
  status.value = accepted.status
  progress.value = action === 'run'
    ? 'Queued for sandbox run'
    : action === 'hint'
      ? 'Queued for a hint'
      : 'Queued for sandbox evaluation'
  reconnectAttempts = 0
  const generation = ++pollGeneration
  socket?.close()
  connectSocket(accepted.submissionId, generation)
  void pollSubmission(accepted.submissionId, generation)
}

async function requestFeedback() {
  if (!selected.value || busyAction.value !== null) return
  busyAction.value = 'submit'
  error.value = ''
  feedback.value = ''
  solvedByAssignmentId.value[selected.value.id] = null
  progress.value = 'Submitting code'
  terminalOutput.value = ''
  status.value = 'PENDING'
  try {
    const accepted = await submitSource(selected.value.id, code.value, backend.value)
    trackSubmission(accepted, 'submit')
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : 'Could not submit code.'
    busyAction.value = null
  }
}

async function requestCodeHint() {
  if (!selected.value || busyAction.value !== null) return
  busyAction.value = 'hint'
  error.value = ''
  feedback.value = ''
  try {
    const accepted = await requestHint(selected.value.id, code.value, backend.value)
    trackSubmission(accepted, 'hint')
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : 'Could not request a hint.'
    busyAction.value = null
  }
}

async function runCode() {
  if (!selected.value || busyAction.value !== null) return
  busyAction.value = 'run'
  error.value = ''
  terminalOutput.value = ''
  feedback.value = ''
  try {
    const accepted = await runSource(selected.value.id, code.value, backend.value)
    trackSubmission(accepted, 'run')
  } catch (cause) {
    terminalOutput.value = cause instanceof Error ? cause.message : 'Run is not available yet.'
    busyAction.value = null
  }
}

function verdictIcon(assignmentId: string): string {
  const verdict = solvedByAssignmentId.value[assignmentId]
  return verdict === true ? 'mdi-check-circle' : verdict === false ? 'mdi-close-circle' : 'mdi-circle-outline'
}

function verdictColor(assignmentId: string): string {
  const verdict = solvedByAssignmentId.value[assignmentId]
  return verdict === true ? 'success' : verdict === false ? 'error' : 'grey'
}

function verdictLabel(assignmentId: string): string {
  const verdict = solvedByAssignmentId.value[assignmentId]
  return verdict === true ? 'Task judged correctly solved' : verdict === false ? 'Task judged not solved' : 'Task has not been judged yet'
}

function formatTerminalOutput(output: unknown): string {
  if (!output || typeof output !== 'object') return output ? JSON.stringify(output, null, 2) : 'No terminal output.'
  const result = output as {
    compilation?: { stdout?: string; stderr?: string }
    testResults?: Array<{ stdout?: string; stderr?: string; exitCode?: number | null }>
  }
  const lines = [result.compilation?.stdout, result.compilation?.stderr]
    .filter((line): line is string => Boolean(line))
  for (const test of result.testResults ?? []) {
    if (test.stdout) lines.push(test.stdout)
    if (test.stderr) lines.push(test.stderr)
    if (test.exitCode !== null && test.exitCode !== undefined) lines.push(`Exit code: ${test.exitCode}`)
  }
  return lines.join('\n') || JSON.stringify(output, null, 2)
}

onMounted(async () => {
  if (authenticated.value) {
    try {
      await loadAssignments()
    } catch {
      logout()
    }
  }
})

onBeforeUnmount(() => {
  pollGeneration++
  window.clearTimeout(reconnectTimer)
  socket?.close()
})
</script>

<template>
  <v-app>
    <v-main>
      <v-container fluid class="app-shell">
        <v-alert v-if="!authenticated" type="info" variant="tonal" class="auth-panel">
          <div class="text-h6 mb-4">SWCP code feedback</div>
          <v-form @submit.prevent="authenticate">
            <v-text-field v-model="username" label="Username" autocomplete="username" required />
            <v-text-field
              v-model="password"
              label="Password"
              type="password"
              :autocomplete="authMode === 'register' ? 'new-password' : 'current-password'"
              :minlength="authMode === 'register' ? 5 : undefined"
              required
            />
            <v-alert v-if="authError" type="error" variant="tonal" class="mb-3">{{ authError }}</v-alert>
            <div class="d-flex ga-2">
              <v-btn type="submit" color="primary" :loading="authLoading">
                {{ authMode === 'login' ? 'Sign in' : 'Create student account' }}
              </v-btn>
              <v-btn variant="text" @click="authMode = authMode === 'login' ? 'register' : 'login'">
                {{ authMode === 'login' ? 'Register' : 'Sign in instead' }}
              </v-btn>
            </div>
          </v-form>
        </v-alert>

        <template v-else>
          <div class="d-flex justify-end mb-3">
            <v-btn variant="text" @click="logout">Sign out</v-btn>
          </div>
          <v-row>
            <v-col cols="12" md="3">
              <AssignmentList
                :assignments="assignments"
                :selected-id="selectedId"
                @select="selectAssignment"
              />
            </v-col>

            <v-col cols="12" md="9">
              <v-card v-if="selected" class="mb-2">
                <v-card-title>{{ selected.title }}</v-card-title>
                <v-card-text class="d-flex align-center ga-2">
                  <span>{{ selected.description }}</span>
                  <v-tooltip :text="verdictLabel(selected.id)">
                    <template #activator="{ props }">
                      <v-icon
                        v-bind="props"
                        :icon="verdictIcon(selected.id)"
                        :color="verdictColor(selected.id)"
                        :aria-label="verdictLabel(selected.id)"
                      />
                    </template>
                  </v-tooltip>
                </v-card-text>
              </v-card>

              <v-select
                v-model="backend"
                :items="[{ title: 'Azure AI Foundry', value: 'azure' }, { title: 'Local vLLM', value: 'local' }]"
                label="Feedback model"
                item-title="title"
                item-value="value"
                class="backend-select"
              />
              <v-alert v-if="progress" type="info" variant="tonal" class="mb-2">
                {{ status }} · {{ progress }}
                <span v-if="submissionId" class="ml-2">{{ submissionId }}</span>
              </v-alert>

              <v-row class="mt-2">
                <v-col cols="12" md="7">
                  <CodeEditorBox
                    v-model="code"
                    :busy-action="busyAction"
                    :terminal-output="terminalOutput"
                    @submit="requestFeedback"
                    @hint="requestCodeHint"
                    @run="runCode"
                  />
                </v-col>
                <v-col cols="12" md="5">
                  <AiFeedbackpanel
                    :feedback="feedback"
                    :loading="loading"
                    :error="error"
                    @retry="requestFeedback"
                  />
                </v-col>
              </v-row>
            </v-col>
          </v-row>
        </template>
      </v-container>
    </v-main>
  </v-app>
</template>

<style scoped>
.app-shell {
  max-width: 1600px;
}

.auth-panel {
  max-width: 520px;
  margin: 10vh auto;
}

.backend-select {
  max-width: 320px;
}
</style>