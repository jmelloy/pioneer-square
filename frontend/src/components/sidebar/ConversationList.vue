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
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useGuildStore } from '../../stores/guild'
import { useConversationsStore } from '../../stores/conversations'
import type { Conversation, ConversationStatus } from '../../types'
import ConversationListRow from './ConversationListRow.vue'

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

const searchResults = ref<Conversation[] | null>(null)
const searching = ref(false)
const searchError = ref('')

const filteredConversations = computed(() =>
  (searchResults.value ?? conversationsStore.conversations)
    .map((result) => conversationsStore.conversations.find((c) => c.id === result.id) ?? result)
    .filter((conversation) => !statusFilter.value || conversation.status === statusFilter.value),
)

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
  try {
    await conversationsStore.fetchConversations(guildId)
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
