<template>
  <div class="thread-list">
    <div class="list-toolbar">
      <div class="status-filter">
        <button
          v-for="opt in FILTER_OPTIONS"
          :key="opt.value"
          class="filter-btn"
          :class="{ active: statusFilter === opt.value }"
          @click="setFilter(opt.value)"
        >
          {{ opt.label }}
        </button>
      </div>
      <button class="new-thread-btn" @click="showNewModal = true">+ New</button>
    </div>

    <div class="rows">
      <div v-if="loading && threadsStore.threads.length === 0" class="empty-state">Loading…</div>
      <div v-else-if="threadsStore.threads.length === 0" class="empty-state">No threads yet</div>
      <ThreadListRow v-for="thread in threadsStore.threads" :key="thread.id" :thread="thread" />
    </div>

    <NewThreadModal
      v-if="showNewModal"
      :creating="creating"
      @close="showNewModal = false"
      @create="onCreate"
    />
  </div>
</template>

<script setup lang="ts">
import { inject, onMounted, ref, watch } from 'vue'
import { useGuildStore } from '../../stores/guild'
import { useThreadsStore } from '../../stores/threads'
import { useUiStore } from '../../stores/ui'
import type { ThreadStatus } from '../../types'
import ThreadListRow from './ThreadListRow.vue'
import NewThreadModal from '../NewThreadModal.vue'

const FILTER_OPTIONS: Array<{ value: ThreadStatus | undefined; label: string }> = [
  { value: undefined, label: 'All' },
  { value: 'active', label: 'Active' },
  { value: 'archived', label: 'Archived' },
  { value: 'closed', label: 'Closed' },
]

const guildStore = useGuildStore()
const threadsStore = useThreadsStore()
const uiStore = useUiStore()

const switchMobileTab = inject<(tab: string) => void>('switchMobileTab', () => {})

const loading = ref(false)
const statusFilter = ref<ThreadStatus | undefined>(undefined)
const showNewModal = ref(false)
const creating = ref(false)

async function load() {
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  loading.value = true
  try {
    await threadsStore.fetchThreads(guildId, statusFilter.value)
  } finally {
    loading.value = false
  }
}

function setFilter(value: ThreadStatus | undefined) {
  statusFilter.value = value
  load()
}

async function onCreate(name: string) {
  const guildId = guildStore.currentGuild?.id
  if (!guildId || creating.value) return
  creating.value = true
  try {
    const thread = await threadsStore.createThread(guildId, { name: name.trim() || undefined })
    showNewModal.value = false
    uiStore.selectThread(thread.id)
    uiStore.openThreadTab(thread.id)
    switchMobileTab('work')
  } finally {
    creating.value = false
  }
}

onMounted(load)

watch(() => guildStore.currentGuild?.id, load)
</script>

<style scoped>
.thread-list {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.list-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--color-brass-dark);
  flex-shrink: 0;
}

.status-filter {
  display: flex;
  gap: 4px;
}

.filter-btn {
  font-family: var(--font-pixel);
  font-size: 5px;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  padding: 4px 6px;
  background: var(--color-bg);
  border: 1px solid var(--color-brass-dark);
  color: var(--color-text-dim);
  cursor: pointer;
  transition: all 0.12s;
}

.filter-btn:hover {
  color: var(--color-brass);
}

.filter-btn.active {
  color: var(--color-brass-light);
  border-color: var(--color-brass);
  background: rgba(232, 170, 0, 0.1);
}

.new-thread-btn {
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  padding: 5px 8px;
  background: var(--color-bg);
  border: 1px solid var(--color-brass);
  color: var(--color-brass-light);
  cursor: pointer;
  flex-shrink: 0;
  transition: all 0.12s;
}

.new-thread-btn:hover {
  background: rgba(232, 170, 0, 0.12);
}

.rows {
  flex: 1;
  overflow-y: auto;
}

.empty-state {
  padding: 24px 12px;
  text-align: center;
  color: var(--color-text-dim);
  font-size: 11px;
  font-style: italic;
}
</style>
