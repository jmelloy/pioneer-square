<template>
  <p v-if="!tasks.length" class="empty">No tasks yet</p>
  <ul v-else class="task-rows">
    <li v-for="task in tasks" :key="task.id">
      <button class="task-row" @click="emit('select-task', task.id)">
        <span class="check" :class="{ done: task.state === 'done' }" aria-hidden="true">{{
          task.state === 'done' ? '✓' : ''
        }}</span>
        <span class="name">{{ task.name || task.id }}</span>
        <StatusBadge
          :label="tasksStore.stateLabel(task.state)"
          :color="tasksStore.stateColor(task.state)"
        />
        <span class="assignee">{{
          task.worker_id ? agentsStore.workerDisplayName(task.worker_id) : '—'
        }}</span>
      </button>
    </li>
  </ul>
</template>

<script setup lang="ts">
import { useTasksStore } from '../../stores/tasks'
import { useAgentsStore } from '../../stores/agents'
import StatusBadge from './StatusBadge.vue'
import type { Task } from '../../types'

defineProps<{ tasks: Task[] }>()
const emit = defineEmits<{ 'select-task': [id: string] }>()

const tasksStore = useTasksStore()
const agentsStore = useAgentsStore()
</script>

<style scoped>
.task-rows {
  list-style: none;
}
.task-row {
  display: grid;
  grid-template-columns: 22px minmax(0, 1fr) auto minmax(0, 9em);
  align-items: center;
  gap: 12px;
  width: 100%;
  padding: 10px 14px;
  background: none;
  border: none;
  border-bottom: 1px solid var(--color-bg-tertiary);
  color: var(--color-text);
  font-family: var(--font-mono);
  font-size: 14px;
  text-align: left;
  cursor: pointer;
}
li:last-child .task-row {
  border-bottom: none;
}
.task-row:hover {
  background: rgba(232, 170, 0, 0.07);
}
.check {
  width: 20px;
  height: 20px;
  display: grid;
  place-items: center;
  border: 2px solid var(--color-text-dim);
  border-radius: 3px;
  font-weight: bold;
}
.check.done {
  background: var(--color-green);
  border-color: var(--color-green);
  color: var(--color-bg);
}
.name,
.assignee {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.assignee {
  color: var(--color-text-dim);
}
.empty {
  padding: 14px;
  color: var(--color-text-dim);
  font-size: 13px;
}
@media (max-width: 600px) {
  .task-row {
    grid-template-columns: 22px minmax(0, 1fr) auto;
  }
  .assignee {
    display: none;
  }
}
</style>
