import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useUiStore } from './ui'
import { api } from '../utils/api'
import type { ChatMessage, ConversationThread, ThreadStatus } from '../types'

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

// Thread-per-conversation architecture (epic #1160, issue #1169). Wraps the
// routes in backend/routes/threads.py and handles live WebSocket push events
// (thread-created, thread-updated) matching the pattern used by agents/tasks/usage stores.
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

  // Thread's own message history (#1175), oldest first — separate from the
  // guild-wide `guildStore.messages` feed so ThreadDetailPanel can show just
  // this conversation instead of the flat comms pane.
  async function fetchThreadMessages(guildId: string, threadId: string): Promise<ChatMessage[]> {
    try {
      return await api<ChatMessage[]>(`/api/guilds/${guildId}/threads/${threadId}/messages`)
    } catch (e) {
      console.error('Failed to fetch thread messages', e)
      return []
    }
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

  /**
   * Handle incoming WebSocket messages for thread lifecycle events.
   * Mirrors the pattern used by agents.ts and tasks.ts stores.
   */

  function handleWebSocketMessage(data: any) {
    switch (data.type) {
      case 'thread-created': {
        const thread: ConversationThread = {
          id: data.threadId,
          conversation_id: data.conversationId,
          discord_thread_id: null,
          name: data.name || null,
          status: data.status || 'active',
          created_at: data.createdAt || new Date().toISOString(),
          updated_at: data.createdAt || new Date().toISOString(),
        }
        _upsertThread(thread)
        break
      }
      case 'thread-updated': {
        const idx = threads.value.findIndex((t) => t.id === data.threadId)
        if (idx >= 0) {
          const existing = threads.value[idx]
          threads.value[idx] = {
            ...existing,
            ...(data.status != null && { status: data.status }),
            ...(data.discordThreadId != null && { discord_thread_id: data.discordThreadId }),
            updated_at: new Date().toISOString(),
          }
        }
        break
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
    fetchThreadMessages,
    createThread,
    archiveThread,
    closeThread,
    clearThreads,
    handleWebSocketMessage,
    statusLabel,
    statusColor,
  }
})
