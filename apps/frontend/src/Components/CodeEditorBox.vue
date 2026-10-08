<script setup lang="ts">
import { CodeEditor } from 'monaco-editor-vue3'

const code = defineModel<string>({ required: true })

defineProps<{
  busyAction: 'hint' | 'run' | 'submit' | null
  terminalOutput: string
}>()
defineEmits<{ submit: []; hint: []; run: [] }>()
</script>

<template>
  <div class="editor-stack">
    <v-card class="code-editor-card" elevation="4">
      <v-card-title>Code box</v-card-title>

      <v-card-text>
        <CodeEditor v-model:value="code" language="C" theme="vs-dark" height="400px" />
      </v-card-text>

      <v-card-actions class="action-row">
        <v-btn variant="outlined" :loading="busyAction === 'hint'" :disabled="busyAction !== null" @click="$emit('hint')">
          Hint
        </v-btn>
        <v-btn variant="outlined" :loading="busyAction === 'run'" :disabled="busyAction !== null" @click="$emit('run')">
          Run
        </v-btn>
        <v-spacer />
        <v-btn variant="outlined" :loading="busyAction === 'submit'" :disabled="busyAction !== null" @click="$emit('submit')">
          Submit
        </v-btn>
      </v-card-actions>
    </v-card>

    <v-card class="terminal-card" elevation="2">
      <v-card-title>Terminal feedback</v-card-title>
      <v-card-text>
        <pre v-if="terminalOutput" class="terminal-output">{{ terminalOutput }}</pre>
        <span v-else class="text-medium-emphasis">Run output will appear here.</span>
      </v-card-text>
    </v-card>
  </div>
</template>

<style scoped>
.editor-stack {
  display: grid;
  gap: 12px;
}

.code-editor-card {
  width: 100%;
}

.action-row {
  flex-wrap: wrap;
  gap: 8px;
}

.terminal-card {
  min-height: 128px;
}

.terminal-output {
  min-height: 48px;
  margin: 0;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
  font-family: monospace;
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
