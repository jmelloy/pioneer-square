import { beforeEach, describe, expect, it, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useThreadsStore } from '../threads'

// Mock the api utility
vi.mock('../../utils/api', () => ({
  api: vi.fn(),
}))

describe('useThreadsStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  describe('handleWebSocketMessage', () => {
    it('inserts a new thread on thread-created', () => {
      const store = useThreadsStore()
      store.handleWebSocketMessage({
        type: 'thread-created',
        threadId: 'thr-abc',
        conversationId: 42,
        userId: 'user-1',
        name: 'Deploy pipeline',
        status: 'active',
        createdAt: '2025-07-01T00:00:00Z',
      })

      expect(store.threads).toHaveLength(1)
      expect(store.threads[0]).toMatchObject({
        id: 'thr-abc',
        conversation_id: 42,
        name: 'Deploy pipeline',
        status: 'active',
        discord_thread_id: null,
        created_at: '2025-07-01T00:00:00Z',
      })
    })

    it('does not duplicate thread on repeated thread-created', () => {
      const store = useThreadsStore()
      const msg = {
        type: 'thread-created',
        threadId: 'thr-abc',
        conversationId: 42,
        name: 'Thread A',
        status: 'active',
        createdAt: '2025-07-01T00:00:00Z',
      } as const

      store.handleWebSocketMessage(msg)
      store.handleWebSocketMessage(msg)

      expect(store.threads).toHaveLength(1)
    })

    it('updates thread status on thread-updated', () => {
      const store = useThreadsStore()
      store.threads.push({
        id: 'thr-abc',
        conversation_id: 42,
        discord_thread_id: null,
        name: 'Thread A',
        status: 'active',
        created_at: '2025-07-01T00:00:00Z',
        updated_at: '2025-07-01T00:00:00Z',
      })

      store.handleWebSocketMessage({
        type: 'thread-updated',
        threadId: 'thr-abc',
        status: 'archived',
      })

      expect(store.threads[0].status).toBe('archived')
    })

    it('updates discord_thread_id on thread-updated', () => {
      const store = useThreadsStore()
      store.threads.push({
        id: 'thr-abc',
        conversation_id: 42,
        discord_thread_id: null,
        name: 'Thread A',
        status: 'active',
        created_at: '2025-07-01T00:00:00Z',
        updated_at: '2025-07-01T00:00:00Z',
      })

      store.handleWebSocketMessage({
        type: 'thread-updated',
        threadId: 'thr-abc',
        discordThreadId: '123456789',
      })

      expect(store.threads[0].discord_thread_id).toBe('123456789')
    })

    it('ignores thread-updated for unknown thread', () => {
      const store = useThreadsStore()
      store.handleWebSocketMessage({
        type: 'thread-updated',
        threadId: 'thr-unknown',
        status: 'closed',
      })

      expect(store.threads).toHaveLength(0)
    })

    it('ignores unrelated message types', () => {
      const store = useThreadsStore()
      store.handleWebSocketMessage({ type: 'task-created', taskId: 't-1', state: 'pending' })
      expect(store.threads).toHaveLength(0)
    })
  })

  describe('statusLabel / statusColor', () => {
    it('returns label for known statuses', () => {
      const store = useThreadsStore()
      expect(store.statusLabel('active')).toBe('active')
      expect(store.statusLabel('archived')).toBe('archived')
      expect(store.statusLabel('closed')).toBe('closed')
    })

    it('returns color for known statuses', () => {
      const store = useThreadsStore()
      expect(store.statusColor('active')).toBe('green')
      expect(store.statusColor('archived')).toBe('amber')
      expect(store.statusColor('closed')).toBe('dim')
    })

    it('returns input for unknown status', () => {
      const store = useThreadsStore()
      expect(store.statusLabel('unknown')).toBe('unknown')
      expect(store.statusColor('unknown')).toBe('dim')
    })
  })

  describe('clearThreads', () => {
    it('clears the threads array', () => {
      const store = useThreadsStore()
      store.threads.push({
        id: 'thr-abc',
        conversation_id: 42,
        discord_thread_id: null,
        name: 'Thread A',
        status: 'active',
        created_at: '2025-07-01T00:00:00Z',
        updated_at: '2025-07-01T00:00:00Z',
      })

      store.clearThreads()
      expect(store.threads).toHaveLength(0)
    })
  })
})
