<template>
  <BaseCard title="Tasks" :count="tasks.length">
    <p v-if="!tasks.length" class="empty">No tasks yet</p>
    <ul v-else class="rows">
      <li v-for="task in tasks" :key="task.id">
        <button class="row" @click="emit('select-task', task.id)">
          <span class="badge" :class="'badge-' + stateColor(task.state)">{{
            stateLabel(task.state)
          }}</span>
          <span class="name">{{ task.name || task.id }}</span>
          <span v-if="task.issue_number" class="meta">#{{ task.issue_number }}</span>
          <span class="meta">{{ formatRelative(task.created_at) }}</span>
        </button>
      </li>
    </ul>
  </BaseCard>
</template>

<script setup lang="ts">
import BaseCard from './BaseCard.vue'
import { formatRelative } from '../utils/format'
import type { Task } from '../types'

defineProps<{
  tasks: Task[]
  stateLabel: (state: string) => string
  stateColor: (state: string) => string
}>()
const emit = defineEmits<{ 'select-task': [id: string] }>()
</script>

<style scoped>
.rows {
  list-style: none;
}
.row {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 6px 12px;
  background: none;
  border: none;
  border-bottom: 1px solid var(--color-bg-tertiary);
  color: var(--color-text);
  font-family: var(--font-mono);
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}
.row:hover {
  background: rgba(232, 170, 0, 0.07);
}
.name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.meta {
  font-size: 11px;
  color: var(--color-text-dim);
  white-space: nowrap;
}
.badge {
  flex-shrink: 0;
  min-width: 64px;
  padding: 1px 4px;
  font-size: 10px;
  text-align: center;
  text-transform: uppercase;
  border: 1px solid currentColor;
  color: var(--color-text-dim);
}
.badge-green {
  color: var(--color-green);
}
.badge-blue {
  color: var(--color-blue);
}
.badge-amber {
  color: var(--color-amber);
}
.badge-teal {
  color: var(--color-teal);
}
.badge-red {
  color: var(--color-red);
}
.badge-orange {
  color: var(--color-orange);
}
.empty {
  padding: 12px;
  font-size: 12px;
  color: var(--color-text-dim);
}
</style>
