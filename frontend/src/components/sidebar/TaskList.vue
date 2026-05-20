<template>
  <div class="tasks-list">
    <div v-if="tasksStore.tasks.length === 0" class="empty-state">No tasks yet</div>

    <template v-for="group in groupedTasks" :key="group.label">
      <div class="date-separator">
        <span class="date-label">{{ group.label }}</span>
        <span class="date-count">{{ group.tasks.length }}</span>
      </div>
      <div
        v-for="task in group.tasks"
        :key="task.id"
        class="task-item"
        :class="{ selected: tasksStore.selectedTaskId === task.id }"
        @click="$emit('open-task', task.id)"
      >
        <div class="task-top">
          <span
            class="task-dot"
            :class="'dot-' + (task.state || 'pending').replace(/[^a-z]/g, '-')"
          ></span>
          <span class="task-name">{{ task.name || task.id }}</span>
        </div>
        <div class="task-meta">
          <span class="task-state">{{ tasksStore.stateLabel(task.state) }}</span>
          <span class="task-time">{{ formatRelative(task.created_at) }}</span>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useTasksStore } from '../../stores/tasks'
import { formatRelative } from '../../utils/format'
import type { Task } from '../../types'

defineEmits<{ (e: 'open-task', id: string): void }>()

const tasksStore = useTasksStore()

const groupedTasks = computed(() => {
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const yesterday = new Date(today)
  yesterday.setDate(yesterday.getDate() - 1)
  const weekAgo = new Date(today)
  weekAgo.setDate(weekAgo.getDate() - 7)

  const groupMap = new Map<string, Task[]>()

  for (const task of tasksStore.tasks) {
    const d = task.created_at ? new Date(task.created_at) : new Date()
    const taskDay = new Date(d.getFullYear(), d.getMonth(), d.getDate())

    let label: string
    if (taskDay >= today) label = 'Today'
    else if (taskDay >= yesterday) label = 'Yesterday'
    else if (taskDay >= weekAgo) label = d.toLocaleDateString('en-US', { weekday: 'long' })
    else label = d.toLocaleDateString('en-US', { month: 'long', year: 'numeric' })

    if (!groupMap.has(label)) groupMap.set(label, [])
    groupMap.get(label)!.push(task)
  }

  return Array.from(groupMap.entries()).map(([label, tasks]) => ({ label, tasks }))
})
</script>

<style scoped>
.tasks-list {
  flex: 1;
  overflow-y: auto;
  padding: 0;
}

.empty-state {
  padding: 24px 12px;
  text-align: center;
  color: var(--color-text-dim);
  font-size: 11px;
  font-style: italic;
}

.date-separator {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 12px 4px;
  position: sticky;
  top: 0;
  background: var(--color-bg-secondary);
  border-bottom: 1px solid var(--color-brass-dark);
  z-index: 1;
}

.date-label {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-brass-dark);
  letter-spacing: 1.5px;
  text-transform: uppercase;
}

.date-count {
  font-size: 9px;
  color: var(--color-text-dim);
  background: var(--color-bg);
  padding: 1px 5px;
  border-radius: 2px;
}

.task-item {
  padding: 8px 12px;
  cursor: pointer;
  border-bottom: 1px solid rgba(255, 255, 255, 0.03);
  transition: background 0.12s;
}

.task-item:hover {
  background: rgba(232, 170, 0, 0.06);
}

.task-item.selected {
  background: rgba(232, 170, 0, 0.12);
  border-left: 3px solid var(--color-brass);
  padding-left: 9px;
}

.task-top {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 3px;
}

.task-dot {
  width: 6px;
  height: 6px;
  border-radius: 2px;
  flex-shrink: 0;
}
.dot-pending {
  background: var(--color-text-dim);
}
.dot-planning {
  background: var(--color-blue);
}
.dot-working {
  background: var(--color-green);
  animation: pulse 0.5s infinite;
}
.dot-awaiting-review {
  background: var(--color-amber);
  animation: pulse 1.5s infinite;
}
.dot-done {
  background: var(--color-teal);
}
.dot-failed {
  background: var(--color-red);
}
.dot-follow-up,
.dot-followup {
  background: var(--color-orange);
  animation: pulse 0.8s infinite;
}

.task-name {
  font-size: 11px;
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1;
}

.task-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-left: 12px;
}

.task-state {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-brass-dark);
  letter-spacing: 0.5px;
  text-transform: uppercase;
}

.task-time {
  font-size: 9px;
  color: var(--color-text-dim);
  flex-shrink: 0;
}

@keyframes pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.3;
  }
}
</style>
