import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useUiStore } from './ui'
import { api } from '../utils/api'
import type { ConversationThread, ThreadStatus, WSInbound } from '../types'

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

// Foreman-owned thread lifecycle (epic #1160, issue #1167). A Thread is
// created/reused server-side as a side effect of the Foreman handling a
// message (backend/foreman/thread_service.py) — this store never originates
// a thread itself, it only reads REST snapshots and applies WS pushes
// (thread-created/thread-updated), matching the agents/tasks/usage store
// pattern.
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

  function handleWebSocketMessage(data: WSInbound) {
    if (data.type === 'thread-created') {
      _upsertThread({
        id: data.threadId,
        conversation_id: data.conversationId,
        discord_thread_id: null,
        name: data.name ?? null,
        status: data.status,
        created_at: data.createdAt,
        updated_at: data.createdAt,
      })
    } else if (data.type === 'thread-updated') {
      const idx = threads.value.findIndex((t) => t.id === data.threadId)
      if (idx < 0) return
      if (data.deletedAt) {
        threads.value.splice(idx, 1)
        if (uiStore.selectedThreadId === data.threadId) uiStore.closeThread(data.threadId)
        return
      }
      const thread = threads.value[idx]
      threads.value[idx] = {
        ...thread,
        status: data.status ?? thread.status,
        discord_thread_id: data.discordThreadId ?? thread.discord_thread_id,
        updated_at: new Date().toISOString(),
      }
    }
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
    archiveThread,
    closeThread,
    handleWebSocketMessage,
    clearThreads,
    statusLabel,
    statusColor,
  }
})
