<template>
  <div
    class="thread-item"
    :class="{ selected: isSelected, 'status-active': thread.status === 'active' }"
    @click="$emit('select', thread.id)"
  >
    <div class="item-indicator">
      <span class="status-dot" :class="'dot-' + thread.status"></span>
    </div>
    <div class="item-body">
      <div class="item-title-row">
        <span class="item-name">{{ thread.name || 'Unnamed thread' }}</span>
        <code class="item-id">{{ thread.id }}</code>
      </div>
      <div class="item-meta">
        <span class="meta-conversation">conv #{{ thread.conversation_id }}</span>
        <span v-if="thread.discord_thread_id" class="meta-discord">
          <span class="discord-icon">⌘</span> linked
        </span>
        <span class="meta-time">{{ formatRelative(thread.updated_at) }}</span>
      </div>
    </div>
    <div class="item-status">
      <span class="status-badge" :class="'badge-' + thread.status">
        {{ statusLabel }}
      </span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useThreadsStore } from '../../stores/threads'
import { formatRelative } from '../../utils/format'
import type { ConversationThread } from '../../types'

const props = defineProps<{
  thread: ConversationThread
  isSelected?: boolean
}>()

defineEmits<{
  (e: 'select', id: string): void
}>()

const threadsStore = useThreadsStore()
const statusLabel = computed(() => threadsStore.statusLabel(props.thread.status))
</script>

<style scoped>
.thread-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  cursor: pointer;
  border-bottom: 1px solid rgba(232, 170, 0, 0.06);
  transition:
    background 0.15s,
    border-color 0.15s;
}

.thread-item:hover {
  background: rgba(232, 170, 0, 0.06);
}

.thread-item.selected {
  background: rgba(232, 170, 0, 0.12);
  border-left: 3px solid var(--color-brass);
  padding-left: 11px;
}

.item-indicator {
  flex-shrink: 0;
  display: flex;
  align-items: center;
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 2px;
}

.dot-active {
  background: var(--color-green);
  box-shadow: 0 0 6px rgba(51, 221, 136, 0.5);
  animation: pulse 2s ease-in-out infinite;
}

.dot-archived {
  background: var(--color-amber);
  opacity: 0.7;
}

.dot-closed {
  background: var(--color-text-dim);
  opacity: 0.5;
}

.item-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.item-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.item-name {
  font-size: 12px;
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1;
  min-width: 0;
}

.item-id {
  font-family: var(--font-mono);
  font-size: 9px;
  color: var(--color-text-dim);
  opacity: 0.6;
  flex-shrink: 0;
}

.item-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 10px;
  color: var(--color-text-dim);
}

.meta-discord {
  color: var(--color-teal);
}

.discord-icon {
  font-size: 9px;
}

.meta-time {
  margin-left: auto;
  font-size: 9px;
  opacity: 0.7;
}

.item-status {
  flex-shrink: 0;
}

.status-badge {
  font-family: var(--font-pixel);
  font-size: 5px;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  padding: 2px 6px;
  border-radius: 2px;
}

.badge-active {
  background: rgba(51, 221, 136, 0.15);
  color: var(--color-green);
}

.badge-archived {
  background: rgba(255, 204, 0, 0.15);
  color: var(--color-amber);
}

.badge-closed {
  background: rgba(255, 255, 255, 0.06);
  color: var(--color-text-dim);
}

@keyframes pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.5;
  }
}
</style>
