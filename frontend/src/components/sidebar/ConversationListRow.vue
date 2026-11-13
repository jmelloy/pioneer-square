<template>
  <div class="conversation-item">
    <div
      class="conversation-row"
      :class="{ selected: isSelected }"
      :aria-expanded="tasks.length ? expanded : undefined"
      @click="tasks.length ? (expanded = !expanded) : open()"
    >
      <span class="collapse-icon">{{ tasks.length ? (expanded ? '▾' : '▸') : '' }}</span>
      <div class="conversation-body">
        <div class="conversation-line">
          <span class="conversation-dot" :class="'dot-' + conversation.status"></span>
          <span class="conversation-name">{{ conversation.name || conversation.id }}</span>
          <span v-if="tasks.length" class="task-count" :title="taskCount + ' tasks'">
            {{ taskCount }}
          </span>
          <span class="status-pill" :class="'status-' + conversation.status">
            {{ conversationsStore.statusLabel(conversation.status) }}
          </span>
          <button
            class="open-btn"
            :title="'Open conversation ' + conversation.id"
            @click.stop="open()"
          >
            Open ↗
          </button>
        </div>
        <div v-if="conversation.preview" class="conversation-preview">
          {{ conversation.preview }}
        </div>
        <div class="conversation-meta">
          <span class="conversation-time">{{ formatRelative(conversation.updated_at) }}</span>
          <span v-if="conversation.participants?.length" class="conversation-participants">
            · {{ conversation.participants.join(', ') }}
          </span>
        </div>
      </div>
    </div>
    <div v-if="expanded && tasks.length" class="conversation-tasks">
      <TaskTreeRow v-for="task in tasks" :key="task.id" :task="task" :depth="0" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useUiStore } from '../../stores/ui'
import { useConversationsStore } from '../../stores/conversations'
import { formatRelative } from '../../utils/format'
import type { Conversation, TaskTreeNode } from '../../types'
import TaskTreeRow from './TaskTreeRow.vue'

const props = withDefaults(
  defineProps<{
    conversation: Conversation
    tasks?: TaskTreeNode[]
  }>(),
  { tasks: () => [] },
)

const uiStore = useUiStore()
const conversationsStore = useConversationsStore()

const TERMINAL_STATES = new Set(['done', 'failed', 'cancelled', 'error'])
function hasActive(tasks: TaskTreeNode[]): boolean {
  return tasks.some((t) => !TERMINAL_STATES.has(t.state) || hasActive(t.children))
}
function count(tasks: TaskTreeNode[]): number {
  return tasks.reduce((n, t) => n + 1 + count(t.children), 0)
}

// ponytail: expansion is per-mount, not persisted; conversations with live work start open.
const expanded = ref(hasActive(props.tasks))
const taskCount = computed(() => count(props.tasks))
const isSelected = computed(() => uiStore.selectedConversationId === props.conversation.id)

function open() {
  uiStore.selectConversation(props.conversation.id)
}
</script>

<style scoped>
.conversation-item {
  border-bottom: 1px solid rgba(255, 255, 255, 0.03);
}

.conversation-row {
  display: flex;
  align-items: flex-start;
  gap: 5px;
  padding: 7px 10px;
  cursor: pointer;
  transition: background 0.12s;
  min-width: 0;
}

.conversation-row:hover {
  background: rgba(232, 170, 0, 0.06);
}

.conversation-row.selected {
  background: rgba(232, 170, 0, 0.12);
  border-left: 3px solid var(--color-brass);
}

.collapse-icon {
  font-size: 9px;
  color: var(--color-text-dim);
  flex-shrink: 0;
  width: 10px;
  user-select: none;
  line-height: 14px;
}

.conversation-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.conversation-line {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
}

.conversation-dot {
  width: 6px;
  height: 6px;
  border-radius: 2px;
  flex-shrink: 0;
}

.dot-active {
  background: var(--color-green);
  animation: pulse 1.5s infinite;
}
.dot-archived {
  background: var(--color-amber);
}
.dot-closed {
  background: var(--color-text-dim);
}

.conversation-name {
  font-size: 11px;
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1;
  min-width: 0;
}

.conversation-preview,
.conversation-meta {
  font-size: 10px;
  color: var(--color-text-dim);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.conversation-meta {
  font-size: 9px;
}

.task-count {
  font-size: 9px;
  color: var(--color-text-dim);
  background: var(--color-bg);
  padding: 1px 5px;
  border-radius: 2px;
  flex-shrink: 0;
}

.status-pill {
  font-family: var(--font-pixel);
  font-size: 5px;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  padding: 1px 4px;
  border-radius: 2px;
  flex-shrink: 0;
}

.status-active {
  background: rgba(80, 200, 120, 0.2);
  color: var(--color-green);
}
.status-archived {
  background: rgba(232, 170, 0, 0.2);
  color: var(--color-amber);
}
.status-closed {
  background: rgba(255, 255, 255, 0.08);
  color: var(--color-text-dim);
}

.open-btn {
  font-size: 9px;
  padding: 1px 4px;
  background: none;
  border: 1px solid var(--color-brass-dark);
  color: var(--color-brass);
  cursor: pointer;
  flex-shrink: 0;
}

.open-btn:hover {
  color: var(--color-brass-light);
  border-color: var(--color-brass);
}

@keyframes pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.4;
  }
}
</style>
