import { beforeEach, describe, expect, it, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useConversationsStore } from '../conversations'

// Mock the api utility
vi.mock('../../utils/api', () => ({
  api: vi.fn(),
}))

describe('useConversationsStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  describe('handleWebSocketMessage', () => {
    it('inserts a new conversation on conversation-created', () => {
      const store = useConversationsStore()
      store.handleWebSocketMessage({
        type: 'conversation-created',
        conversationId: 42,
        userId: 'user-1',
        name: 'Deploy pipeline',
        status: 'active',
        createdAt: '2025-07-01T00:00:00Z',
      })

      expect(store.conversations).toHaveLength(1)
      expect(store.conversations[0]).toMatchObject({
        id: 42,
        name: 'Deploy pipeline',
        status: 'active',
        discord_thread_id: null,
        created_at: '2025-07-01T00:00:00Z',
      })
    })

    it('does not duplicate conversation on repeated conversation-created', () => {
      const store = useConversationsStore()
      const msg = {
        type: 'conversation-created',
        conversationId: 42,
        name: 'Conversation A',
        status: 'active',
        createdAt: '2025-07-01T00:00:00Z',
      } as const

      store.handleWebSocketMessage(msg)
      store.handleWebSocketMessage(msg)

      expect(store.conversations).toHaveLength(1)
    })

    it('updates conversation status on conversation-updated', () => {
      const store = useConversationsStore()
      store.conversations.push({
        id: 42,
        user_id: null,
        discord_thread_id: null,
        name: 'Conversation A',
        status: 'active',
        created_at: '2025-07-01T00:00:00Z',
        updated_at: '2025-07-01T00:00:00Z',
      })

      store.handleWebSocketMessage({
        type: 'conversation-updated',
        conversationId: 42,
        status: 'archived',
      })

      expect(store.conversations[0].status).toBe('archived')
    })

    it('updates discord_thread_id on conversation-updated', () => {
      const store = useConversationsStore()
      store.conversations.push({
        id: 42,
        user_id: null,
        discord_thread_id: null,
        name: 'Conversation A',
        status: 'active',
        created_at: '2025-07-01T00:00:00Z',
        updated_at: '2025-07-01T00:00:00Z',
      })

      store.handleWebSocketMessage({
        type: 'conversation-updated',
        conversationId: 42,
        discordThreadId: '123456789',
      })

      expect(store.conversations[0].discord_thread_id).toBe('123456789')
    })

    it('ignores conversation-updated for unknown conversation', () => {
      const store = useConversationsStore()
      store.handleWebSocketMessage({
        type: 'conversation-updated',
        conversationId: 99,
        status: 'closed',
      })

      expect(store.conversations).toHaveLength(0)
    })

    it('ignores unrelated message types', () => {
      const store = useConversationsStore()
      store.handleWebSocketMessage({ type: 'task-created', taskId: 't-1', state: 'pending' })
      expect(store.conversations).toHaveLength(0)
    })
  })

  describe('statusLabel / statusColor', () => {
    it('returns label for known statuses', () => {
      const store = useConversationsStore()
      expect(store.statusLabel('active')).toBe('active')
      expect(store.statusLabel('archived')).toBe('archived')
      expect(store.statusLabel('closed')).toBe('closed')
    })

    it('returns color for known statuses', () => {
      const store = useConversationsStore()
      expect(store.statusColor('active')).toBe('green')
      expect(store.statusColor('archived')).toBe('amber')
      expect(store.statusColor('closed')).toBe('dim')
    })

    it('returns input for unknown status', () => {
      const store = useConversationsStore()
      expect(store.statusLabel('unknown')).toBe('unknown')
      expect(store.statusColor('unknown')).toBe('dim')
    })
  })

  describe('clearConversations', () => {
    it('clears the conversations array', () => {
      const store = useConversationsStore()
      store.conversations.push({
        id: 42,
        user_id: null,
        discord_thread_id: null,
        name: 'Conversation A',
        status: 'active',
        created_at: '2025-07-01T00:00:00Z',
        updated_at: '2025-07-01T00:00:00Z',
      })

      store.clearConversations()
      expect(store.conversations).toHaveLength(0)
    })
  })
})
