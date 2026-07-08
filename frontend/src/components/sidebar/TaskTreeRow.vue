<template>
  <div class="tree-row-group">
    <div
      class="task-row"
      :class="{
        selected: selectedTaskId === task.id,
        'has-indent': depth > 0,
        'issue-root-row': isIssueRoot,
      }"
      :style="{ paddingLeft: `${10 + depth * 14}px` }"
      @click="$emit('open-task', task.id)"
    >
      <span
        v-if="task.children.length && !isIssueRoot"
        class="row-toggle"
        @click.stop="expanded = !expanded"
      >
        {{ expanded ? '▾' : '▸' }}
      </span>
      <span v-else class="row-toggle-spacer"></span>

      <template v-if="isIssueRoot">
        <span class="issue-icon" aria-hidden="true">
          <svg viewBox="0 0 16 16" width="10" height="10" fill="currentColor">
            <path
              d="M8 1.5a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13ZM0 8a8 8 0 1 1 16 0A8 8 0 0 1 0 8Zm9 3a1 1 0 1 1-2 0 1 1 0 0 1 2 0Zm-.25-6.25a.75.75 0 0 0-1.5 0v3.5a.75.75 0 0 0 1.5 0v-3.5Z"
            />
          </svg>
        </span>

        <span class="task-name issue-root-name">{{ task.name || task.id }}</span>

        <a
          v-if="task.issue_repo && task.issue_number"
          class="issue-link"
          :href="`https://github.com/${task.issue_repo}/issues/${task.issue_number}`"
          target="_blank"
          rel="noopener"
          @click.stop
        >
          {{ task.issue_repo }}#{{ task.issue_number }}
        </a>
      </template>

      <template v-else>
        <span class="task-dot" :class="'dot-' + dotClass(task.state)"></span>

        <span class="task-name">{{ task.name || task.id }}</span>

        <span v-if="task.phase" class="phase-pill" :class="'phase-' + task.phase">
          {{ task.phase }}
        </span>

        <span class="state-pill" :class="'state-' + dotClass(task.state)">
          {{ stateLabel(task.state) }}
        </span>

        <span v-if="task.worker_id && isActiveState(task.state)" class="worker-id">
          {{ task.worker_id.slice(0, 10) }}
        </span>
      </template>
    </div>

    <template v-if="(isIssueRoot || expanded) && task.children.length">
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
import { computed, ref } from 'vue'
import type { TaskTreeNode } from '../../types'

const props = defineProps<{
  task: TaskTreeNode
  depth: number
  selectedTaskId: string | null
}>()

defineEmits<{ (e: 'open-task', id: string): void }>()

const ACTIVE_STATES = new Set(['pending', 'planning', 'working', 'followup'])
const expanded = ref(true)

// Root task of an issue-rooted subtree — never assigned to a worker, has no
// branch/PR, so it gets a dedicated GH-issue-link treatment instead of the
// usual phase/state pills, and is always shown expanded.
const isIssueRoot = computed(() => props.task.phase === 'issue')

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

.issue-root-row {
  background: rgba(232, 170, 0, 0.05);
  border-bottom: 1px solid var(--color-brass-dark);
}

.issue-icon {
  display: flex;
  align-items: center;
  color: var(--color-brass);
  flex-shrink: 0;
}

.issue-root-name {
  font-weight: 600;
}

.issue-link {
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 0.5px;
  color: var(--color-brass);
  background: rgba(232, 170, 0, 0.15);
  padding: 2px 5px;
  border-radius: 2px;
  flex-shrink: 0;
  text-decoration: none;
  white-space: nowrap;
}

.issue-link:hover {
  text-decoration: underline;
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
