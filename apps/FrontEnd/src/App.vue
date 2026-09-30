<script setup lang="ts">
import { ref } from 'vue'
import CodeEditorBox from './Components/CodeEditorBox.vue'
import AiFeedbackpanel from './Components/AiFeedbackpanel.vue'

const code = ref(`#include <stdio.h>\n\nint main(void) {\n    return 0;\n}`)
const feedback = ref('')
const loading = ref(false)
const error = ref('')

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
      <v-container>
        <v-card>
          <v-card-title> C Assignment </v-card-title>

          <v-card-text> Write a C program that add to ints together </v-card-text>
        </v-card>
          <v-row class="editor-container">
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
      </v-container>
    </v-main>
  </v-app>
</template>

<style scoped>
.editor-container {
  margin-top: 20px;
}
</style>
