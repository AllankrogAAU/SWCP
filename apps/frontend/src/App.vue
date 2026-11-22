<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import CodeEditorBox from './Components/CodeEditorBox.vue'
import AiFeedbackpanel from './Components/AiFeedbackpanel.vue'
import AssignmentList from './Components/AssignmentList.vue'
import {
  ApiError,
  cancelSubmission,
  clearAccessToken,
  getAccessToken,
  getAssignments,
  getAssignmentState,
  saveAssignmentState,
  getSubmissionHistory,
  getSubmission,
  login,
  requestHint,
  register,
  runSource,
  setAccessToken,
  streamSubmissionEvents,
  submitSource,
  type Assignment,
  type LlmBackend,
  type SubmissionResponse,
  type SubmissionStatus,
} from './services/api'

const assignments = ref<Assignment[]>([])
const solvedByAssignmentId = ref<Record<string, boolean | null>>({})
const selectedId = ref('')
const selected = computed(() =>
  assignments.value.find((assignment) => assignment.id === selectedId.value),
)
const code = ref('#include <stdio.h>\n\nint main(void) {\n    return 0;\n}')
const lastSavedAt = ref('')
const standardInput = ref('')
const feedback = ref('')
const busyAction = ref<'hint' | 'run' | 'submit' | null>(null)
const error = ref('')
const terminalOutput = ref('')
const status = ref<SubmissionStatus | ''>('')
const backend = ref<LlmBackend>('azure')
const username = ref('')
const password = ref('')
const authMode = ref<'login' | 'register'>('login')
const authenticated = ref(Boolean(getAccessToken()))
const authLoading = ref(false)
const authError = ref('')
const submissionId = ref('')
const canceling = ref(false)
const loading = computed(() => busyAction.value === 'submit' || busyAction.value === 'hint')
const loadingAssignment = ref(false)
let stopStream: (() => void) | undefined
let pollGeneration = 0
let assignmentLoadGeneration = 0
let autosaveTimer: number | undefined

async function selectAssignment(id: string) {
  const generation = ++assignmentLoadGeneration

  window.clearTimeout(autosaveTimer)
  autosaveTimer = undefined
  selectedId.value = id
  lastSavedAt.value = ''
  standardInput.value = ''
  loadingAssignment.value = true

  try {
    const state = await getAssignmentState(id)

    if (generation !== assignmentLoadGeneration) {
      return
    }

    code.value = state.currentCode
    lastSavedAt.value = state.updatedAtUtc
  } catch (error) {
    console.error('FAILED TO LOAD STATE', error)
    if (generation !== assignmentLoadGeneration) {
      return
    }

    code.value = '#include <stdio.h>\n\nint main(void) {\n    return 0;\n}'
  } finally {
    if (generation === assignmentLoadGeneration) {
      loadingAssignment.value = false
    }
  }

  feedback.value = ''
  error.value = ''
  terminalOutput.value = ''
}

async function loadAssignments() {
  const [assignmentRows, submissions] = await Promise.all([
    getAssignments(),
    getSubmissionHistory(),
  ])
  assignments.value = assignmentRows
  const latestSubmitByAssignment: Record<string, boolean | null> = {}
  for (const submission of submissions) {
    if (submission.action === 'submit' && !(submission.assignmentId in latestSubmitByAssignment)) {
      latestSubmitByAssignment[submission.assignmentId] = submission.taskSolved
    }
  }
  solvedByAssignmentId.value = latestSubmitByAssignment

  const firstId = assignments.value[0]?.id

  if (firstId) {
    console.log('CALLING SELECT ASSIGNMENT')
    await selectAssignment(firstId)
  }

  const activeSubmission = submissions.find(
    (submission) => submission.status !== 'COMPLETED' && submission.status !== 'FAILED',
  )
  if (activeSubmission && busyAction.value === null) {
    busyAction.value = activeSubmission.action
    trackSubmission({
      submissionId: activeSubmission.submissionId,
      status: activeSubmission.status,
    })
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
  window.clearTimeout(autosaveTimer)
  autosaveTimer = undefined

  clearAccessToken()
  authenticated.value = false
  assignments.value = []
  submissionId.value = ''
  busyAction.value = null
  pollGeneration++
  stopStream?.()
  stopStream = undefined
}

function applyCompletion(result: SubmissionResponse) {
  status.value = result.status
  if (result.status === 'COMPLETED') {
    if (result.action === 'submit') {
      solvedByAssignmentId.value[result.assignmentId] = result.taskSolved
    }
    error.value = ''
    feedback.value =
      result.action === 'run'
        ? ''
        : (result.llmFeedback ?? JSON.stringify(result.sandboxOutput, null, 2))
    terminalOutput.value = formatTerminalOutput(result.sandboxOutput)
    busyAction.value = null
    stopStream?.()
    stopStream = undefined
    pollGeneration++
    return
  }
  if (result.status === 'FAILED') {
    error.value = result.errorMessage ?? 'The submission failed.'
    terminalOutput.value = error.value
    if (result.action === 'submit') {
      solvedByAssignmentId.value[result.assignmentId] = result.taskSolved
    }
    busyAction.value = null
    stopStream?.()
    stopStream = undefined
    pollGeneration++
    return
  }
}

async function pollSubmission(id: string, generation: number) {
  while (generation === pollGeneration && busyAction.value !== null) {
    try {
      const result = await getSubmission(id)
      if (generation !== pollGeneration) return
      if (result.status === 'COMPLETED' || result.status === 'FAILED') {
        applyCompletion(result)
        return
      }
      status.value = result.status
    } catch (cause) {
      if (generation !== pollGeneration) return
      error.value = cause instanceof Error ? cause.message : 'Could not retrieve submission status.'
      busyAction.value = null
      stopStream?.()
      stopStream = undefined
      return
    }

    await new Promise((resolve) => window.setTimeout(resolve, 2000))
  }
}

function trackSubmission(accepted: { submissionId: string; status: SubmissionStatus }) {
  submissionId.value = accepted.submissionId
  status.value = accepted.status
  const generation = ++pollGeneration
  stopStream?.()
  stopStream = streamSubmissionEvents(
    accepted.submissionId,
    (result) => {
      if (generation !== pollGeneration) return
      applyCompletion(result)
    },
    () => {
      // SSE connection error: fallback polling continues
    },
  )
  void pollSubmission(accepted.submissionId, generation)
}

function resumeIfConflict(cause: unknown): boolean {
  if (cause instanceof ApiError && cause.status === 409 && cause.activeSubmissionId) {
    error.value = cause.message
    trackSubmission({
      submissionId: cause.activeSubmissionId,
      status: cause.submissionStatus ?? 'PENDING',
    })
    return true
  }
  return false
}

async function cancelActiveSubmission() {
  if (!submissionId.value || canceling.value) return
  canceling.value = true
  try {
    await cancelSubmission(submissionId.value)
    pollGeneration++
    stopStream?.()
    stopStream = undefined
    busyAction.value = null
    status.value = 'FAILED'
    error.value = 'Canceled by user.'
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : 'Could not cancel submission.'
  } finally {
    canceling.value = false
  }
}

async function requestFeedback() {
  if (!selected.value || busyAction.value !== null) return
  busyAction.value = 'submit'
  error.value = ''
  feedback.value = ''
  solvedByAssignmentId.value[selected.value.id] = null
  terminalOutput.value = ''
  status.value = 'PENDING'
  try {
    const accepted = await submitSource(selected.value.id, code.value, backend.value)
    trackSubmission(accepted)
  } catch (cause) {
    if (!resumeIfConflict(cause)) {
      error.value = cause instanceof Error ? cause.message : 'Could not submit code.'
      busyAction.value = null
    }
  }
}

async function requestCodeHint() {
  if (!selected.value || busyAction.value !== null) return
  busyAction.value = 'hint'
  error.value = ''
  feedback.value = ''
  try {
    const accepted = await requestHint(selected.value.id, code.value, backend.value)
    trackSubmission(accepted)
  } catch (cause) {
    if (!resumeIfConflict(cause)) {
      error.value = cause instanceof Error ? cause.message : 'Could not request a hint.'
      busyAction.value = null
    }
  }
}

async function runCode() {
  if (!selected.value || busyAction.value !== null) return
  busyAction.value = 'run'
  error.value = ''
  terminalOutput.value = ''
  feedback.value = ''
  try {
    const accepted = await runSource(
      selected.value.id,
      code.value,
      backend.value,
      standardInput.value,
    )
    trackSubmission(accepted)
  } catch (cause) {
    if (!resumeIfConflict(cause)) {
      terminalOutput.value = cause instanceof Error ? cause.message : 'Run is not available yet.'
      busyAction.value = null
    }
  }
}

function verdictIcon(assignmentId: string): string {
  const verdict = solvedByAssignmentId.value[assignmentId]
  return verdict === true
    ? 'mdi-check-circle'
    : verdict === false
      ? 'mdi-close-circle'
      : 'mdi-circle-outline'
}

function verdictColor(assignmentId: string): string {
  const verdict = solvedByAssignmentId.value[assignmentId]
  return verdict === true ? 'success' : verdict === false ? 'error' : 'grey'
}

function verdictLabel(assignmentId: string): string {
  const verdict = solvedByAssignmentId.value[assignmentId]
  return verdict === true
    ? 'Task judged correctly solved'
    : verdict === false
      ? 'Task judged not solved'
      : 'Task has not been judged yet'
}

function formatTerminalOutput(output: unknown): string {
  if (!output || typeof output !== 'object')
    return output ? JSON.stringify(output, null, 2) : 'No terminal output.'
  const result = output as {
    compilation?: { stdout?: string; stderr?: string }
    testResults?: Array<{ stdout?: string; stderr?: string; exitCode?: number | null }>
  }
  const lines = [result.compilation?.stdout, result.compilation?.stderr].filter(
    (line): line is string => Boolean(line),
  )
  for (const test of result.testResults ?? []) {
    if (test.stdout) lines.push(test.stdout)
    if (test.stderr) lines.push(test.stderr)
    if (test.exitCode !== null && test.exitCode !== undefined)
      lines.push(`Exit code: ${test.exitCode}`)
  }
  return lines.join('\n') || JSON.stringify(output, null, 2)
}

watch(code, () => {
  if (loadingAssignment.value) {
    return
  }

  const assignmentId = selectedId.value
  if (!assignmentId) return
  const codeToSave = code.value

  window.clearTimeout(autosaveTimer)

  autosaveTimer = window.setTimeout(async () => {
    try {
      await saveAssignmentState(assignmentId, codeToSave)
      if (selectedId.value === assignmentId) {
        lastSavedAt.value = new Date().toISOString()
      }
    } catch (error) {
      console.error('AUTOSAVE FAILED', error)
    }
  }, 1000)
})

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
  window.clearTimeout(autosaveTimer)
  stopStream?.()
  stopStream = undefined
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
            <v-alert v-if="authError" type="error" variant="tonal" class="mb-3">{{
              authError
            }}</v-alert>
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
                :items="[
                  { title: 'Azure AI Foundry', value: 'azure' },
                  { title: 'Local vLLM', value: 'local' },
                ]"
                label="Feedback model"
                item-title="title"
                item-value="value"
                class="backend-select"
              />

              <v-row class="mt-2">
                <v-col cols="12" md="7">
                  <div v-if="lastSavedAt" class="text-caption text-grey mb-2">
                    Last saved:
                    {{ new Date(lastSavedAt).toLocaleString() }}
                  </div>

                  <CodeEditorBox
                    v-model="code"
                    v-model:standard-input="standardInput"
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
                    :canceling="canceling"
                    @retry="requestFeedback"
                    @cancel="cancelActiveSubmission"
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
