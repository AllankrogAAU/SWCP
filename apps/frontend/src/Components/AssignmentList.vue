<script setup lang="ts">
import type { Assignment } from '../services/api'

defineProps<{
    assignments: Assignment[]
    selectedId: string
}>()

defineEmits<{
    select: [id: string]
}>()
</script>

<template>
  <v-card elevation="4">
    <v-card-title>Assignments</v-card-title>
    <v-list>
      <v-list-item
        v-for="a in assignments"
        :key="a.id"
        :title="a.title"
        :active="a.id === selectedId"
        @click="$emit('select', a.id)"
      />
    </v-list>
  </v-card>
</template>