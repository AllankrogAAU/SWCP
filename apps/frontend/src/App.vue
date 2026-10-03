<script setup lang="ts">
import { ref, computed} from 'vue'
import CodeEditorBox from './Components/CodeEditorBox.vue'
import AiFeedbackpanel from './Components/AiFeedbackpanel.vue'
import AssignmentList from './Components/AssignmentList.vue'
import { assignments } from './data/assignments.js'

const selectedId = ref(assignments[0]?.id ?? 0)
const selected = computed(() => assignments.find((a) => a.id == selectedId.value))

const code = ref(selected.value?.starterCode ?? '')
const feedback = ref('')
const loading = ref(false)
const error = ref('')

function selectAssignment(id: number) {
  selectedId.value = id
  code.value = selected.value?.starterCode ?? ''
  feedback.value = ''
  error.value = ''
}

// fake for now; replace with the real API call later
async function requestFeedback() {
  loading.value = true
  error.value = ''
  await new Promise((r) => setTimeout(r, 1000))
  feedback.value = `Fake feedback.\nYour code has ${code.value.length} characters.`
  loading.value = false
}
</script>

<template>
  <v-app>
    <v-main>
      <v-container fluid>
        <v-row>
          <v-col cols="12" md="3">
            <AssignmentList
              :assignments="assignments"
              :selected-id="selectedId"
              @select="selectAssignment"
            />
          </v-col>

          <v-col cols="12" md="9">
            <v-card v-if="selected">
              <v-card-title>{{ selected.title }}</v-card-title>
              <v-card-text>{{ selected.description }}</v-card-text>
            </v-card>

            <v-row class="mt-2">
              <v-col cols="12" md="7">
                <CodeEditorBox v-model="code" :loading="loading" @submit="requestFeedback" />
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
      </v-container>
    </v-main>
  </v-app>
</template>

<style scoped>
.editor-container {
  margin-top: 20px;
}
</style>
