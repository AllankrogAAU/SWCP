<script setup lang="ts">
import { ref } from 'vue'
import { CodeEditor } from 'monaco-editor-vue3'

const code = ref(`def hello():
    print("Hello, Oskar!")`)
</script>

<template>
  <v-card class="code-editor-card" elevation="4"
    >¨
    <v-card-title> Code box </v-card-title>

    <v-card-text>
      <CodeEditor v-model:value="code" language="C" theme="vs-dark" height="400px" />
    </v-card-text>

    <v-card-actions>
      <v-btn color="primary"> Run Code </v-btn>
    </v-card-actions>
  </v-card>
</template>

<style scoped>
.code-editor-card {
  width: 100%;
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
