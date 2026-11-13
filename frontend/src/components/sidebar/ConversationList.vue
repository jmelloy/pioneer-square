<template>
  <div class="conversation-list">
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
      <div class="range-filter">
        <select v-model="timeRange" class="filter-select" aria-label="Time range">
          <option v-for="opt in TIME_RANGES" :key="opt.label" :value="opt.hours">
            {{ opt.label }}
          </option>
        </select>
        <select v-model="limit" class="filter-select" aria-label="Number of conversations">
          <option v-for="n in LIMITS" :key="n" :value="n">{{ n ? `Last ${n}` : 'All' }}</option>
        </select>
      </div>
      <input
        v-model="search"
        class="conversation-search"
        type="search"
        aria-label="Search conversation names and messages"
        placeholder="Search names and messages…"
      />
    </div>

    <div class="rows">
      <div v-if="loading || searching" class="empty-state">Loading…</div>
      <div v-else-if="searchError" class="empty-state" role="alert">{{ searchError }}</div>
      <div v-else-if="filteredConversations.length === 0" class="empty-state">
        {{
          search.trim() || conversationsStore.conversations.length
            ? 'No matching conversations.'
            : 'No conversations yet. Send a message to the Foreman to start one.'
        }}
      </div>
      <ConversationListRow
        v-for="conversation in filteredConversations"
        :key="conversation.id"
        :conversation="conversation"
        :tasks="tasksByConversation.get(conversation.id)"
      />
      <div v-if="showUngrouped" class="ungrouped">
        <div class="ungrouped-header" @click="ungroupedOpen = !ungroupedOpen">
          <span class="collapse-icon">{{ ungroupedOpen ? '▾' : '▸' }}</span>
          Ungrouped tasks
        </div>
        <template v-if="ungroupedOpen">
          <TaskTreeRow v-for="task in tree!.ungrouped" :key="task.id" :task="task" :depth="0" />
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useGuildStore } from '../../stores/guild'
import { useConversationsStore } from '../../stores/conversations'
import { useTasksStore } from '../../stores/tasks'
import { api } from '../../utils/api'
import type { Conversation, ConversationStatus, TaskTreeData, TaskTreeNode } from '../../types'
import ConversationListRow from './ConversationListRow.vue'
import TaskTreeRow from './TaskTreeRow.vue'

const TIME_RANGES: Array<{ hours: number; label: string }> = [
  { hours: 0, label: 'Any time' },
  { hours: 24, label: 'Last 24h' },
  { hours: 24 * 7, label: 'Last 7 days' },
  { hours: 24 * 30, label: 'Last 30 days' },
]
const LIMITS = [25, 50, 100, 0]

const FILTER_OPTIONS: Array<{ value: ConversationStatus | undefined; label: string }> = [
  { value: undefined, label: 'All' },
  { value: 'active', label: 'Active' },
  { value: 'archived', label: 'Archived' },
  { value: 'closed', label: 'Closed' },
]

const guildStore = useGuildStore()
const conversationsStore = useConversationsStore()
const loading = ref(false)
const statusFilter = ref<ConversationStatus | undefined>(undefined)
const search = ref('')
const timeRange = ref(0)
const limit = ref(25)
const tasksStore = useTasksStore()
const tree = ref<TaskTreeData | null>(null)
const ungroupedOpen = ref(true)

const searchResults = ref<Conversation[] | null>(null)
const searching = ref(false)
const searchError = ref('')

const filteredConversations = computed(() =>
  (searchResults.value ?? conversationsStore.conversations)
    .map((result) => conversationsStore.conversations.find((c) => c.id === result.id) ?? result)
    .filter((conversation) => !statusFilter.value || conversation.status === statusFilter.value)
    .filter(
      (conversation) =>
        !timeRange.value ||
        Date.now() - new Date(conversation.updated_at).getTime() <= timeRange.value * 3_600_000,
    )
    .slice(0, limit.value || undefined),
)

const tasksByConversation = computed(
  () =>
    new Map<number, TaskTreeNode[]>(tree.value?.conversations.map((c) => [c.id, c.tasks]) ?? []),
)

const showUngrouped = computed(
  () => !!tree.value?.ungrouped.length && !statusFilter.value && !search.value.trim(),
)

async function fetchTree() {
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  try {
    tree.value = await api<TaskTreeData>(`/guilds/${guildId}/tasks/tree`)
  } catch (e) {
    console.error('Failed to fetch task tree', e)
  }
}

// Refetch the task tree (debounced) when any task appears or changes state.
let treeTimer: ReturnType<typeof setTimeout> | undefined
watch(
  () => tasksStore.tasks.map((t) => t.state).join(','),
  () => {
    clearTimeout(treeTimer)
    treeTimer = setTimeout(fetchTree, 1500)
  },
)
onUnmounted(() => clearTimeout(treeTimer))

watch([search, () => guildStore.currentGuild?.id], ([value, guildId], _, onCleanup) => {
  searchResults.value = null
  searching.value = false
  searchError.value = ''
  const query = value.trim()
  if (!query || !guildId) return

  searching.value = true
  searchResults.value = []
  let cancelled = false
  const timer = setTimeout(async () => {
    try {
      const results = await conversationsStore.searchConversations(guildId, query)
      if (!cancelled) searchResults.value = results
    } catch (error) {
      if (!cancelled) {
        searchError.value = 'Search failed. Please try again.'
        console.error('Failed to search conversations', error)
      }
    } finally {
      if (!cancelled) searching.value = false
    }
  }, 250)
  onCleanup(() => {
    cancelled = true
    clearTimeout(timer)
  })
})

async function load() {
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  loading.value = true
  tree.value = null
  try {
    await Promise.all([conversationsStore.fetchConversations(guildId), fetchTree()])
  } finally {
    loading.value = false
  }
}

function setFilter(value: ConversationStatus | undefined) {
  statusFilter.value = value
}

onMounted(load)

watch(() => guildStore.currentGuild?.id, load)
</script>

<style scoped>
.conversation-list {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.list-toolbar {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--color-brass-dark);
  flex-shrink: 0;
}

.conversation-search {
  width: 100%;
  box-sizing: border-box;
  padding: 6px 8px;
  background: var(--color-bg);
  border: 1px solid var(--color-brass-dark);
  color: var(--color-text);
}

.range-filter {
  display: flex;
  gap: 4px;
}

.filter-select {
  flex: 1;
  font-size: 10px;
  padding: 3px 4px;
  background: var(--color-bg);
  border: 1px solid var(--color-brass-dark);
  color: var(--color-text);
}

.ungrouped-header {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 6px 10px;
  cursor: pointer;
  font-size: 11px;
  font-style: italic;
  color: var(--color-text-dim);
  border-bottom: 1px solid var(--color-brass-dark);
}

.collapse-icon {
  font-size: 9px;
  width: 10px;
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
