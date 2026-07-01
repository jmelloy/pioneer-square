<template>
  <div class="tree-row-group">
    <div
      class="task-row"
      :class="{ selected: selectedTaskId === task.id, 'has-indent': depth > 0 }"
      :style="{ paddingLeft: `${10 + depth * 14}px` }"
      @click="$emit('open-task', task.id)"
    >
      <span v-if="task.children.length" class="row-toggle" @click.stop="expanded = !expanded">
        {{ expanded ? '▾' : '▸' }}
      </span>
      <span v-else class="row-toggle-spacer"></span>

      <span class="task-dot" :class="'dot-' + dotClass(task.state)"></span>

      <span class="task-name">{{ task.name || task.description || task.id }}</span>

      <span v-if="task.phase" class="phase-pill" :class="'phase-' + task.phase">
        {{ task.phase }}
      </span>

      <span class="state-pill" :class="'state-' + dotClass(task.state)">
        {{ stateLabel(task.state) }}
      </span>

      <span v-if="task.worker_id && isActiveState(task.state)" class="worker-id">
        {{ task.worker_id.slice(0, 10) }}
      </span>
    </div>

    <template v-if="expanded && task.children.length">
      <TaskTreeRow
        v-for="child in task.children"
        :key="child.id"
        :task="child"
        :depth="depth + 1"
        :selected-task-id="selectedTaskId"
        @open-task="$emit('open-task', $event)"
      />
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import type { TaskTreeNode } from '../../types'

defineProps<{
  task: TaskTreeNode
  depth: number
  selectedTaskId: string | null
}>()

defineEmits<{ (e: 'open-task', id: string): void }>()

const ACTIVE_STATES = new Set(['pending', 'planning', 'working', 'followup'])
const expanded = ref(true)

function dotClass(state: string): string {
  return (state || 'pending').replace(/[^a-z]/g, '-')
}

const STATE_LABELS: Record<string, string> = {
  pending: 'pending',
  planning: 'planning',
  working: 'working',
  'awaiting-review': 'review',
  done: 'done',
  failed: 'failed',
  followup: 'follow-up',
  cancelled: 'cancelled',
}

function stateLabel(state: string): string {
  return STATE_LABELS[state] || state
}

function isActiveState(state: string): boolean {
  return ACTIVE_STATES.has(state)
}
</script>

<style scoped>
.tree-row-group {
  display: flex;
  flex-direction: column;
}

.task-row {
  display: flex;
  align-items: center;
  gap: 5px;
  padding-top: 5px;
  padding-bottom: 5px;
  padding-right: 8px;
  cursor: pointer;
  border-bottom: 1px solid rgba(255, 255, 255, 0.03);
  transition: background 0.12s;
  min-width: 0;
}

.task-row:hover {
  background: rgba(232, 170, 0, 0.06);
}

.task-row.selected {
  background: rgba(232, 170, 0, 0.12);
  border-left: 3px solid var(--color-brass);
}

.row-toggle {
  font-size: 9px;
  color: var(--color-text-dim);
  flex-shrink: 0;
  width: 12px;
  text-align: center;
  cursor: pointer;
  user-select: none;
}

.row-toggle:hover {
  color: var(--color-brass);
}

.row-toggle-spacer {
  width: 12px;
  flex-shrink: 0;
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
.dot-failed,
.dot-cancelled {
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
  min-width: 0;
}

.phase-pill {
  font-family: var(--font-pixel);
  font-size: 5px;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  padding: 1px 3px;
  border-radius: 2px;
  flex-shrink: 0;
}

.phase-plan {
  background: rgba(100, 149, 237, 0.25);
  color: var(--color-blue);
}
.phase-execute {
  background: rgba(80, 200, 120, 0.2);
  color: var(--color-green);
}
.phase-review {
  background: rgba(232, 170, 0, 0.2);
  color: var(--color-amber);
}
.phase-followup {
  background: rgba(255, 165, 0, 0.2);
  color: var(--color-orange);
}

.state-pill {
  font-family: var(--font-pixel);
  font-size: 5px;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  padding: 1px 3px;
  border-radius: 2px;
  flex-shrink: 0;
  opacity: 0.75;
}

.state-working,
.state-planning {
  color: var(--color-green);
}
.state-awaiting-review {
  color: var(--color-amber);
}
.state-done {
  color: var(--color-teal);
}
.state-failed,
.state-cancelled {
  color: var(--color-red);
}
.state-pending {
  color: var(--color-text-dim);
}

.worker-id {
  font-size: 9px;
  color: var(--color-text-dim);
  flex-shrink: 0;
  font-family: monospace;
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
