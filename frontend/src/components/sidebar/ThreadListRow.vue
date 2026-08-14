<template>
  <div
    class="thread-row"
    :class="{ selected: isSelected }"
    @click="uiStore.selectThread(thread.id)"
  >
    <span class="thread-dot" :class="'dot-' + thread.status"></span>
    <span class="thread-name">{{ thread.name || thread.id }}</span>
    <span class="status-pill" :class="'status-' + thread.status">
      {{ threadsStore.statusLabel(thread.status) }}
    </span>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useUiStore } from '../../stores/ui'
import { useThreadsStore } from '../../stores/threads'
import type { ConversationThread } from '../../types'

const props = defineProps<{
  thread: ConversationThread
}>()

const uiStore = useUiStore()
const threadsStore = useThreadsStore()

const isSelected = computed(() => uiStore.selectedThreadId === props.thread.id)
</script>

<style scoped>
.thread-row {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 7px 10px;
  cursor: pointer;
  border-bottom: 1px solid rgba(255, 255, 255, 0.03);
  transition: background 0.12s;
  min-width: 0;
}

.thread-row:hover {
  background: rgba(232, 170, 0, 0.06);
}

.thread-row.selected {
  background: rgba(232, 170, 0, 0.12);
  border-left: 3px solid var(--color-brass);
}

.thread-dot {
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

.thread-name {
  font-size: 11px;
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1;
  min-width: 0;
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
