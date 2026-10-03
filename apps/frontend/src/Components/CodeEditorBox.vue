<script setup lang="ts">
import { CodeEditor } from 'monaco-editor-vue3'

const code = defineModel<string>({ required: true })

defineProps<{ loading: boolean }>()
defineEmits<{ submit: [] }>()

function exportCode() {
  // Put the code from the editor into a JSON object
  const data = {
    code: code.value,
  }

  // Convert the object to JSON
  const json = JSON.stringify(data, null, 2)

  // Create a JSON file
  const blob = new Blob([json], {
    type: 'application/json',
  })

  // Create a temporary URL for the file
  const url = URL.createObjectURL(blob)

  // Create a download link
  const link = document.createElement('a')
  link.href = url
  link.download = 'code.json'

  // Download the file
  link.click()

  // Clean up
  URL.revokeObjectURL(url)
}
</script>

<template>
  <v-card class="code-editor-card" elevation="4"
    >¨
    <v-card-title> Code box </v-card-title>

    <v-card-text>
      <CodeEditor v-model:value="code" language="C" theme="vs-dark" height="400px" />
    </v-card-text>

    <v-card-actions>
      <v-btn color="primary" @click="exportCode"> Show payload </v-btn>
    </v-card-actions>
  </v-card>
</template>

<style scoped>
.code-editor-card {
  width: 100%;
  height: 100%;
}

.code-editor {
  width: 100%;
  min-height: 400px;

  background: #1e1e1e;
  color: #ffffff;

  font-family: monospace;
  font-size: 16px;
  line-height: 1.5;

  padding: 16px;
  border-radius: 6px;

  resize: vertical;
  outline: none;
  border: none;
}
</style>
