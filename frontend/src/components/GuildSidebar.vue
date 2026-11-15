<template>
  <aside class="sidebar panel-bg">
    <div class="sidebar-heading">
      <h2><span aria-hidden="true">☷</span> Conversations</h2>
      <span class="active-count">● {{ activeConversationCount }} active</span>
      <button
        type="button"
        class="filters-toggle"
        :aria-expanded="!filtersCollapsed"
        aria-controls="conversation-filters"
        :aria-label="filtersCollapsed ? 'Show filters and search' : 'Hide filters and search'"
        @click="toggleFilters"
      >
        <span class="filters-chevron" :class="{ collapsed: filtersCollapsed }" aria-hidden="true"
          >▾</span
        >
      </button>
    </div>

    <div class="tab-content">
      <ConversationList :filters-collapsed="filtersCollapsed" />
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
import { computed, inject, ref, watch } from 'vue'
import { useGuildStore } from '../stores/guild'
import { useUiStore } from '../stores/ui'
import { useConversationsStore } from '../stores/conversations'
import ConversationList from './sidebar/ConversationList.vue'
import CostSummary from './sidebar/CostSummary.vue'

const guildStore = useGuildStore()
const uiStore = useUiStore()
const conversationsStore = useConversationsStore()

const switchMobileTab = inject<(tab: string) => void>('switchMobileTab', () => {})

const FILTERS_STORAGE_KEY = 'conversationFiltersCollapsed'
const storedFilters = localStorage.getItem(FILTERS_STORAGE_KEY)
// No saved preference: start collapsed on phones, where the toolbar eats the viewport.
const filtersCollapsed = ref(
  storedFilters === null
    ? (window.matchMedia?.('(max-width: 767px)').matches ?? false)
    : storedFilters === 'true',
)
function toggleFilters() {
  filtersCollapsed.value = !filtersCollapsed.value
  localStorage.setItem(FILTERS_STORAGE_KEY, String(filtersCollapsed.value))
}

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

.filters-toggle {
  min-width: 44px;
  min-height: 44px;
  margin: -12px -10px -12px -4px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: none;
  border: none;
  color: var(--color-brass);
  cursor: pointer;
}

.filters-toggle:hover,
.filters-toggle:focus-visible {
  color: var(--color-brass-light);
}

.filters-chevron {
  font-size: 14px;
  transition: transform 0.2s ease;
}

.filters-chevron.collapsed {
  transform: rotate(-90deg);
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
  font-size: var(--text-small);
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
