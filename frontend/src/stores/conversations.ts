import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useGuildStore } from './guild'
import { useUiStore } from './ui'
import { api } from '../utils/api'
import type { ChatMessage, Conversation, ConversationStatus, WSInbound } from '../types'

const STATUS_LABELS: Record<ConversationStatus, string> = {
  active: 'active',
  archived: 'archived',
  closed: 'closed',
}

const STATUS_COLORS: Record<ConversationStatus, string> = {
  active: 'green',
  archived: 'amber',
  closed: 'dim',
}

// Conversations UI/API surface (#1298, epic #1271). Wraps the routes in
// backend/routes/conversations.py and handles live WebSocket push events
// (conversation-created, conversation-updated) matching the pattern used by
// agents/tasks/usage stores.
export const useConversationsStore = defineStore('conversations', () => {
  const uiStore = useUiStore()
  const conversations = ref<Conversation[]>([])

  function _upsertConversation(conversation: Conversation) {
    const idx = conversations.value.findIndex((c) => c.id === conversation.id)
    if (idx >= 0) {
      conversations.value[idx] = conversation
    } else {
      conversations.value.unshift(conversation)
    }
  }

  async function fetchConversations(guildId: string, status?: ConversationStatus) {
    if (!guildId) return
    try {
      const query = status ? `?status=${encodeURIComponent(status)}` : ''
      conversations.value = await api<Conversation[]>(
        `/api/guilds/${guildId}/conversations${query}`,
      )
    } catch (e) {
      console.error('Failed to fetch conversations', e)
    }
  }

  async function fetchConversation(guildId: string, conversationId: number) {
    const conversation = await api<Conversation>(
      `/api/guilds/${guildId}/conversations/${conversationId}`,
    )
    _upsertConversation(conversation)
    return conversation
  }

  // Conversation's own message history (#1298), oldest first — separate from
  // the guild-wide `guildStore.messages` feed so ConversationDetailPanel can
  // show just this conversation instead of the flat comms pane.
  async function fetchConversationMessages(
    guildId: string,
    conversationId: number,
  ): Promise<ChatMessage[]> {
    try {
      return await api<ChatMessage[]>(
        `/api/guilds/${guildId}/conversations/${conversationId}/messages`,
      )
    } catch (e) {
      console.error('Failed to fetch conversation messages', e)
      return []
    }
  }

  async function closeConversation(guildId: string, conversationId: number) {
    const conversation = await api<Conversation>(
      `/api/guilds/${guildId}/conversations/${conversationId}/close`,
      { method: 'PATCH' },
    )
    _upsertConversation(conversation)
    return conversation
  }

  /**
   * Handle incoming WebSocket messages for conversation lifecycle events.
   * Mirrors the pattern used by agents.ts and tasks.ts stores.
   */

  function handleWebSocketMessage(data: WSInbound) {
    switch (data.type) {
      case 'conversation-created': {
        const conversation: Conversation = {
          id: data.conversationId,
          user_id: data.userId ?? null,
          discord_thread_id: null,
          name: data.name || null,
          status: data.status || 'active',
          created_at: data.createdAt || new Date().toISOString(),
          updated_at: data.createdAt || new Date().toISOString(),
        }
        _upsertConversation(conversation)
        break
      }
      case 'conversation-updated': {
        const idx = conversations.value.findIndex((c) => c.id === data.conversationId)
        if (idx >= 0) {
          const existing = conversations.value[idx]
          conversations.value[idx] = {
            ...existing,
            ...(data.name != null && { name: data.name }),
            ...(data.status != null && { status: data.status }),
            ...(data.discordThreadId != null && { discord_thread_id: data.discordThreadId }),
            updated_at: new Date().toISOString(),
          }
        }
        break
      }
    }
  }

  // Declare interest in every inbound WS frame — see subscribeWS in guild.ts,
  // which owns parsing/validation/routing and dispatches here.
  useGuildStore().subscribeWS(handleWebSocketMessage)

  function clearConversations() {
    conversations.value = []
    uiStore.resetConversationSelection()
  }

  function statusLabel(status: ConversationStatus | string) {
    return STATUS_LABELS[status as ConversationStatus] || status
  }
  function statusColor(status: ConversationStatus | string) {
    return STATUS_COLORS[status as ConversationStatus] || 'dim'
  }

  return {
    conversations,
    fetchConversations,
    fetchConversation,
    fetchConversationMessages,
    closeConversation,
    clearConversations,
    handleWebSocketMessage,
    statusLabel,
    statusColor,
  }
})
