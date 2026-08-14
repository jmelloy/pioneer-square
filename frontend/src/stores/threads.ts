import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useUiStore } from './ui'
import { api } from '../utils/api'
import type { ConversationThread, ThreadStatus } from '../types'

const STATUS_LABELS: Record<ThreadStatus, string> = {
  active: 'active',
  archived: 'archived',
  closed: 'closed',
}

const STATUS_COLORS: Record<ThreadStatus, string> = {
  active: 'green',
  archived: 'amber',
  closed: 'dim',
}

// Thread-per-conversation architecture (epic #1160, issue #1162). Wraps the
// routes in backend/routes/threads.py; there is no WS event for thread
// lifecycle changes yet, so this store is REST-only (unlike tasks.ts, which
// also has a handleWebSocketMessage).
export const useThreadsStore = defineStore('threads', () => {
  const uiStore = useUiStore()
  const threads = ref<ConversationThread[]>([])

  function _upsertThread(thread: ConversationThread) {
    const idx = threads.value.findIndex((t) => t.id === thread.id)
    if (idx >= 0) {
      threads.value[idx] = thread
    } else {
      threads.value.unshift(thread)
    }
  }

  async function fetchThreads(guildId: string, status?: ThreadStatus) {
    if (!guildId) return
    try {
      const query = status ? `?status=${encodeURIComponent(status)}` : ''
      threads.value = await api<ConversationThread[]>(`/api/guilds/${guildId}/threads${query}`)
    } catch (e) {
      console.error('Failed to fetch threads', e)
    }
  }

  async function fetchThread(guildId: string, threadId: string) {
    const thread = await api<ConversationThread>(`/api/guilds/${guildId}/threads/${threadId}`)
    _upsertThread(thread)
    return thread
  }

  async function createThread(
    guildId: string,
    body: { name?: string; conversation_id?: number; user_id?: string } = {},
  ) {
    const thread = await api<ConversationThread>(`/api/guilds/${guildId}/threads`, {
      method: 'POST',
      json: body,
    })
    _upsertThread(thread)
    return thread
  }

  async function archiveThread(guildId: string, threadId: string) {
    const thread = await api<ConversationThread>(
      `/api/guilds/${guildId}/threads/${threadId}/archive`,
      { method: 'PATCH' },
    )
    _upsertThread(thread)
    return thread
  }

  async function closeThread(guildId: string, threadId: string) {
    const thread = await api<ConversationThread>(
      `/api/guilds/${guildId}/threads/${threadId}/close`,
      { method: 'PATCH' },
    )
    _upsertThread(thread)
    return thread
  }

  function clearThreads() {
    threads.value = []
    uiStore.resetThreadSelection()
  }

  function statusLabel(status: ThreadStatus | string) {
    return STATUS_LABELS[status as ThreadStatus] || status
  }
  function statusColor(status: ThreadStatus | string) {
    return STATUS_COLORS[status as ThreadStatus] || 'dim'
  }

  return {
    threads,
    fetchThreads,
    fetchThread,
    createThread,
    archiveThread,
    closeThread,
    clearThreads,
    statusLabel,
    statusColor,
  }
})
