<template>
  <div
    class="conversation-row"
    :class="{ selected: isSelected }"
    @click="uiStore.selectConversation(conversation.id)"
  >
    <span class="conversation-dot" :class="'dot-' + conversation.status"></span>
    <span class="conversation-name">{{ conversation.name || conversation.id }}</span>
    <span class="status-pill" :class="'status-' + conversation.status">
      {{ conversationsStore.statusLabel(conversation.status) }}
    </span>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useUiStore } from '../../stores/ui'
import { useConversationsStore } from '../../stores/conversations'
import type { Conversation } from '../../types'

const props = defineProps<{
  conversation: Conversation
}>()

const uiStore = useUiStore()
const conversationsStore = useConversationsStore()

const isSelected = computed(() => uiStore.selectedConversationId === props.conversation.id)
</script>

<style scoped>
.conversation-row {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 7px 10px;
  cursor: pointer;
  border-bottom: 1px solid rgba(255, 255, 255, 0.03);
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
