<template>
  <div class="thread-list-panel">
    <div class="list-header">
      <div class="header-title">
        <span class="header-icon">✉</span>
        <span class="header-text">THREADS</span>
        <span class="thread-count">{{ threads.length }}</span>
      </div>
      <button class="create-btn" @click="showCreateModal = true" title="New Thread">
        + NEW
      </button>
    </div>

    <div class="list-filters">
      <button
        v-for="opt in FILTER_OPTIONS"
        :key="opt.value ?? 'all'"
        class="filter-chip"
        :class="{ active: statusFilter === opt.value }"
        @click="setFilter(opt.value)"
      >
        {{ opt.label }}
        <span v-if="opt.count !== undefined" class="filter-count">{{ opt.count }}</span>
      </button>
    </div>

    <div class="list-body">
      <div v-if="loading && threads.length === 0" class="state-message">
        <span class="spinner">⟳</span> Loading threads…
      </div>
      <div v-else-if="error" class="state-message error">
        <span>⚠</span> {{ error }}
        <button class="retry-btn" @click="fetchThreads">Retry</button>
      </div>
      <div v-else-if="threads.length === 0" class="state-message empty">
        No threads{{ statusFilter ? ` with status "${statusFilter}"` : '' }}
      </div>
      <div v-else class="thread-items">
        <ThreadItem
          v-for="thread in threads"
          :key="thread.id"
          :thread="thread"
          :is-selected="selectedThread?.id === thread.id"
          @select="selectThread"
        />
      </div>
    </div>

    <NewThreadModal
      v-if="showCreateModal"
      :creating="creating"
      @close="showCreateModal = false"
      @create="onCreateThread"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import type { ThreadStatus } from '../../types'
import { useThreads } from './useThreads'
import ThreadItem from './ThreadItem.vue'
import NewThreadModal from '../NewThreadModal.vue'

const props = defineProps<{
  /** Optional polling interval in ms. */
  pollInterval?: number
}>()

const {
  threads,
  activeThreads,
  archivedThreads,
  closedThreads,
  selectedThread,
  loading,
  error,
  statusFilter,
  fetchThreads,
  createThread,
  selectThread,
  setFilter,
} = useThreads({ pollInterval: props.pollInterval ?? 0 })

const showCreateModal = ref(false)
const creating = ref(false)

const FILTER_OPTIONS = computed(() => [
  { value: undefined as ThreadStatus | undefined, label: 'All', count: threads.value.length },
  { value: 'active' as ThreadStatus, label: 'Active', count: activeThreads.value.length },
  { value: 'archived' as ThreadStatus, label: 'Archived', count: archivedThreads.value.length },
  { value: 'closed' as ThreadStatus, label: 'Closed', count: closedThreads.value.length },
])

async function onCreateThread(name: string) {
  if (creating.value) return
  creating.value = true
  try {
    const thread = await createThread(name)
    if (thread) {
      showCreateModal.value = false
      selectThread(thread.id)
    }
  } finally {
    creating.value = false
  }
}
</script>

<style scoped>
.thread-list-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
  background: var(--color-bg);
}

.list-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  background: var(--color-bg-tertiary);
  border-bottom: 2px solid var(--color-brass-dark);
  flex-shrink: 0;
}

.header-title {
  display: flex;
  align-items: center;
  gap: 8px;
}

.header-icon {
  color: var(--color-brass);
  font-size: 14px;
}

.header-text {
  font-family: var(--font-pixel);
  font-size: 8px;
  color: var(--color-brass-light);
  letter-spacing: 2px;
}

.thread-count {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--color-text-dim);
  background: rgba(232, 170, 0, 0.1);
  border: 1px solid var(--color-brass-dark);
  padding: 1px 5px;
  border-radius: 2px;
}

.create-btn {
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 1px;
  text-transform: uppercase;
  padding: 5px 10px;
  background: transparent;
  border: 1px solid var(--color-brass);
  color: var(--color-brass-light);
  cursor: pointer;
  transition: all 0.15s;
}

.create-btn:hover {
  background: rgba(232, 170, 0, 0.12);
  box-shadow: 0 0 8px rgba(232, 170, 0, 0.2);
}

.list-filters {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 8px 14px;
  border-bottom: 1px solid rgba(232, 170, 0, 0.1);
  flex-shrink: 0;
  flex-wrap: wrap;
}

.filter-chip {
  font-family: var(--font-pixel);
  font-size: 5px;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  padding: 4px 8px;
  background: transparent;
  border: 1px solid var(--color-brass-dark);
  color: var(--color-text-dim);
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 4px;
  transition: all 0.12s;
}

.filter-chip:hover {
  color: var(--color-brass);
  border-color: var(--color-brass);
}

.filter-chip.active {
  color: var(--color-brass-light);
  border-color: var(--color-brass);
  background: rgba(232, 170, 0, 0.1);
}

.filter-count {
  font-family: var(--font-mono);
  font-size: 8px;
  opacity: 0.7;
}

.list-body {
  flex: 1;
  overflow-y: auto;
}

.thread-items {
  display: flex;
  flex-direction: column;
}

.state-message {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 32px 16px;
  font-size: 11px;
  color: var(--color-text-dim);
  font-style: italic;
}

.state-message.error {
  color: var(--color-red);
  font-style: normal;
}

.state-message.empty {
  flex-direction: column;
  gap: 4px;
}

.retry-btn {
  font-family: var(--font-pixel);
  font-size: 6px;
  padding: 4px 8px;
  background: transparent;
  border: 1px solid var(--color-red);
  color: var(--color-red);
  cursor: pointer;
  margin-left: 8px;
}

.retry-btn:hover {
  background: rgba(238, 51, 34, 0.1);
}

.spinner {
  animation: spin 1s linear infinite;
  display: inline-block;
}

@keyframes spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}
</style>
