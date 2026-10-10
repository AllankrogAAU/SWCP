<script setup lang="ts">
defineProps<{
  feedback: string
  loading: boolean
  error: string
}>()

defineEmits<{
  retry: []
}>()
</script>

<template>
  <v-card class="feedback-card" elevation="4">
    <v-card-title>AI feedback</v-card-title>

    <v-card-text>
      <!-- Loading -->
      <div v-if="loading" class="d-flex align-center ga-3">
        <v-progress-circular indeterminate size="24" />
        <span>Reviewing your code...</span>
      </div>

      <!-- Error -->
      <v-alert v-else-if="error" type="error" variant="tonal">
        {{ error }}
        <template #append>
          <v-btn size="small" variant="text" @click="$emit('retry')">Retry</v-btn>
        </template>
      </v-alert>

      <!-- Feedback -->
      <div v-else-if="feedback" class="feedback-text">{{ feedback }}</div>

      <!-- Empty state -->
      <div v-else class="text-medium-emphasis">
        Write some C code and press "Get feedback" to see suggestions here.
      </div>
    </v-card-text>
  </v-card>
</template>

<style scoped>
.feedback-card {
  width: 100%;
  min-height: 200px;
}

.feedback-text {
  white-space: pre-wrap; /* keep the AI's line breaks */
  line-height: 1.6;
}
</style>
