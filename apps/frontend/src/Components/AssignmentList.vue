<script setup lang="ts">
import type { Assignment } from '@/data/assignments'

defineProps<{
  assignments: Assignment[]
  selectedId: number
}>()

defineEmits<{
  select: [id: number]
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
