<script setup lang="ts">
import CodeEditorBox from './Components/CodeEditorBox.vue'
</script>

<template>
  <v-app>
    <v-main>
      <v-container>
        <v-card>
          <v-card-title> Python Assignment </v-card-title>

          <v-card-text> Write a Python program that add to ints together </v-card-text>
        </v-card>

        <div class="editor-container">
          <CodeEditorBox />
        </div>
      </v-container>
    </v-main>
  </v-app>
</template>

<style scoped>
.editor-container {
  margin-top: 20px;
}
</style>
