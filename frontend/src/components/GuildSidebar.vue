<template>
  <aside class="sidebar panel-bg">
    <div class="sidebar-heading">
      <h2><span aria-hidden="true">☷</span> Conversations</h2>
      <span class="active-count">● {{ activeConversationCount }} active</span>
    </div>

    <div class="tab-content">
      <ConversationList />
    </div>

    <div class="sidebar-footer">
      <CostSummary />
      <div class="connection-status" :class="{ connected: isConnected }">
        <span class="status-dot"></span>
        {{ isConnected ? 'Connected' : 'Disconnected' }}
      </div>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, inject, watch } from 'vue'
import { useGuildStore } from '../stores/guild'
import { useUiStore } from '../stores/ui'
import { useConversationsStore } from '../stores/conversations'
import ConversationList from './sidebar/ConversationList.vue'
import CostSummary from './sidebar/CostSummary.vue'

const guildStore = useGuildStore()
const uiStore = useUiStore()
const conversationsStore = useConversationsStore()

const switchMobileTab = inject<(tab: string) => void>('switchMobileTab', () => {})

const isConnected = computed(() => guildStore.isConnected)
const activeConversationCount = computed(
  () => conversationsStore.conversations.filter((c) => c.status === 'active').length,
)

watch(
  () => uiStore.selectedTaskId,
  (id) => {
    if (id) switchMobileTab('work')
  },
)

watch(
  () => uiStore.selectedConversationId,
  (id) => {
    if (id != null) {
      uiStore.openConversationTab(id)
      switchMobileTab('work')
    }
  },
)
</script>

<style scoped>
.sidebar {
  width: 360px;
  min-width: 360px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.tab-content {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.sidebar-footer {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px 12px;
  border-top: 2px solid var(--color-brass-dark);
  background: var(--color-bg-tertiary);
  flex-shrink: 0;
}

.connection-status {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 10px;
  color: var(--color-red);
}

.connection-status.connected {
  color: var(--color-green);
}

.status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
  display: inline-block;
}

.connection-status.connected .status-dot {
  animation: pulse 2s infinite;
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
