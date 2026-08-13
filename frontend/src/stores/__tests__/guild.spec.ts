import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useGuildStore } from '../guild'
import { useAuthStore } from '../auth'

// Minimal WebSocket fake — no real network. Captures the constructor URL,
// exposes hooks to drive open/message/close from tests.
class FakeWebSocket {
  static OPEN = 1
  static CLOSED = 3
  static instances: FakeWebSocket[] = []

  url: string
  readyState = 0
  onopen: ((e: Event) => void) | null = null
  onmessage: ((e: MessageEvent) => void) | null = null
  onclose: ((e: CloseEvent) => void) | null = null
  onerror: ((e: Event) => void) | null = null
  sent: string[] = []
  closed = false

  constructor(url: string) {
    this.url = url
    FakeWebSocket.instances.push(this)
  }

  send(data: string) {
    this.sent.push(data)
  }

  close() {
    this.closed = true
    this.readyState = FakeWebSocket.CLOSED
  }

  simulateOpen() {
    this.readyState = FakeWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  simulateMessage(data: any) {
    this.onmessage?.({
      data: typeof data === 'string' ? data : JSON.stringify(data),
    } as MessageEvent)
  }

  simulateClose(opts: { wasClean?: boolean; code?: number; reason?: string } = {}) {
    this.readyState = FakeWebSocket.CLOSED
    this.onclose?.({
      wasClean: opts.wasClean ?? true,
      code: opts.code ?? 1000,
      reason: opts.reason ?? '',
    } as CloseEvent)
  }
}

describe('useGuildStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket as any)
    // Silence the chatty console output in expected error paths.
    vi.spyOn(console, 'error').mockImplementation(() => {})
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    vi.spyOn(console, 'info').mockImplementation(() => {})
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  describe('connectWebSocket', () => {
    it('opens a ws:// URL when page is http', () => {
      const store = useGuildStore()
      store.connectWebSocket('g-1')
      expect(FakeWebSocket.instances).toHaveLength(1)
      expect(FakeWebSocket.instances[0].url).toMatch(/^ws:\/\/.+\/ws\/g-1$/)
    })

    it('appends the auth login token as a query param', () => {
      const auth = useAuthStore()
      auth.loginToken = 'abc 123'
      const store = useGuildStore()
      store.connectWebSocket('g-1')
      expect(FakeWebSocket.instances[0].url).toContain('?token=abc%20123')
    })

    it('flips isConnected on open and resets reconnectAttempt', () => {
      const store = useGuildStore()
      store.reconnectAttempt = 5
      store.connectWebSocket('g-1')
      FakeWebSocket.instances[0].simulateOpen()
      expect(store.isConnected).toBe(true)
      expect(store.reconnectAttempt).toBe(0)
    })

    it('closes the previous socket when called again', () => {
      const store = useGuildStore()
      store.connectWebSocket('g-1')
      const first = FakeWebSocket.instances[0]
      store.connectWebSocket('g-1')
      expect(first.closed).toBe(true)
      expect(FakeWebSocket.instances).toHaveLength(2)
    })
  })

  describe('incoming WS messages', () => {
    it('appends chat messages to messages array', () => {
      const store = useGuildStore()
      store.connectWebSocket('g-1')
      const ws = FakeWebSocket.instances[0]

      ws.simulateMessage({ type: 'chat', from: 'u1', content: 'hi' })
      expect(store.messages).toHaveLength(1)
      expect(store.messages[0]).toMatchObject({ type: 'chat', from: 'u1', content: 'hi' })
    })

    it('updates currentGuild and guilds list on guild-updated', () => {
      const store = useGuildStore()
      store.currentGuild = { id: 'g-1', name: 'old' }
      store.guilds = [
        { id: 'g-1', name: 'old' },
        { id: 'g-2', name: 'other' },
      ]

      store.connectWebSocket('g-1')
      const ws = FakeWebSocket.instances[0]
      ws.simulateMessage({ type: 'guild-updated', id: 'g-1', name: 'new' })

      expect(store.currentGuild?.name).toBe('new')
      expect(store.guilds[0].name).toBe('new')
      expect(store.guilds[1].name).toBe('other')
    })

    it('drops non-JSON frames without throwing', () => {
      const store = useGuildStore()
      store.connectWebSocket('g-1')
      const ws = FakeWebSocket.instances[0]
      expect(() => ws.simulateMessage('not json')).not.toThrow()
      expect(store.messages).toHaveLength(0)
    })

    it('invokes the per-call onMessage callback', () => {
      const onMessage = vi.fn()
      const store = useGuildStore()
      store.connectWebSocket('g-1', onMessage)
      const ws = FakeWebSocket.instances[0]
      ws.simulateMessage({ type: 'agent-state', state: 'idle' })
      expect(onMessage).toHaveBeenCalledWith({ type: 'agent-state', state: 'idle' })
    })

    it('pushes a system message on task-complete with a PR', () => {
      const store = useGuildStore()
      store.connectWebSocket('g-1')
      FakeWebSocket.instances[0].simulateMessage({
        type: 'task-complete',
        taskId: 't-1',
        workerId: 'w-1',
        prUrl: 'https://example.com/pr/1',
      })

      expect(store.messages).toHaveLength(1)
      expect(store.messages[0]).toMatchObject({
        type: 'chat',
        from: 'system',
        prUrl: 'https://example.com/pr/1',
      })
      expect(store.messages[0].content).toContain('https://example.com/pr/1')
    })

    it('pushes a system message on needs-input', () => {
      const store = useGuildStore()
      store.connectWebSocket('g-1')
      FakeWebSocket.instances[0].simulateMessage({
        type: 'needs-input',
        workerId: 'w-1',
        description: 'pick a branch name',
      })

      expect(store.messages).toHaveLength(1)
      expect(store.messages[0].content).toContain('pick a branch name')
    })

    it('pushes a system message on task-assigned', () => {
      const store = useGuildStore()
      store.connectWebSocket('g-1')
      FakeWebSocket.instances[0].simulateMessage({
        type: 'task-assigned',
        taskId: 't-1',
        workerId: 'w-1',
        name: 'Fix the thing',
      })

      expect(store.messages).toHaveLength(1)
      expect(store.messages[0].content).toContain('Fix the thing')
    })

    it('sets nextPollAt from foreman-poll-status', () => {
      const store = useGuildStore()
      store.connectWebSocket('g-1')
      const before = Date.now()
      FakeWebSocket.instances[0].simulateMessage({ type: 'foreman-poll-status', nextCheckIn: 60 })

      expect(store.nextPollAt).not.toBeNull()
      expect(store.nextPollAt as number).toBeGreaterThanOrEqual(before + 60_000)
    })

    it('clears nextPollAt when foreman-poll-status omits nextCheckIn', () => {
      const store = useGuildStore()
      store.connectWebSocket('g-1')
      FakeWebSocket.instances[0].simulateMessage({ type: 'foreman-poll-status', nextCheckIn: 60 })
      FakeWebSocket.instances[0].simulateMessage({ type: 'foreman-poll-status' })

      expect(store.nextPollAt).toBeNull()
    })
  })

  describe('disconnect / reconnect', () => {
    it('disconnectWebSocket suppresses reconnect on subsequent close', () => {
      vi.useFakeTimers()
      const store = useGuildStore()
      store.connectWebSocket('g-1')
      const ws = FakeWebSocket.instances[0]

      store.disconnectWebSocket()
      ws.simulateClose({ wasClean: false })
      vi.advanceTimersByTime(60_000)

      expect(FakeWebSocket.instances).toHaveLength(1)
      expect(store.isConnected).toBe(false)
      expect(store.reconnectAttempt).toBe(0)
      vi.useRealTimers()
    })

    it('reconnects with backoff after an unclean close', () => {
      vi.useFakeTimers()
      const store = useGuildStore()
      store.connectWebSocket('g-1')
      const ws = FakeWebSocket.instances[0]

      ws.simulateClose({ wasClean: false, code: 1006 })
      expect(store.reconnectAttempt).toBe(1)

      // Backoff is randomized within [0, 1000ms] for attempt 1; advance well past that.
      vi.advanceTimersByTime(31_000)
      expect(FakeWebSocket.instances.length).toBeGreaterThanOrEqual(2)
      vi.useRealTimers()
    })
  })

  describe('sendMessage', () => {
    it('returns false when not connected', () => {
      const store = useGuildStore()
      expect(store.sendMessage({ type: 'chat', content: 'x' })).toBe(false)
    })

    it('serializes and sends when connected', () => {
      const store = useGuildStore()
      store.connectWebSocket('g-1')
      const ws = FakeWebSocket.instances[0]
      ws.simulateOpen()

      const ok = store.sendMessage({ type: 'chat', content: 'hello' })
      expect(ok).toBe(true)
      expect(ws.sent).toHaveLength(1)
      expect(JSON.parse(ws.sent[0])).toMatchObject({ type: 'chat', content: 'hello' })
    })

    it('returns false when the socket is not open', () => {
      const store = useGuildStore()
      store.connectWebSocket('g-1')
      // never call simulateOpen — readyState is still CONNECTING (0)
      const ok = store.sendMessage({ type: 'chat', content: 'hi' })
      expect(ok).toBe(false)
    })
  })

  describe('REST helpers', () => {
    it('loadGuilds populates guilds on success', async () => {
      const fakeGuilds = [{ id: 'g-1', name: 'Lab' }]
      vi.stubGlobal(
        'fetch',
        vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(fakeGuilds) }),
      )
      const store = useGuildStore()
      await store.loadGuilds()
      expect(store.guilds).toEqual(fakeGuilds)
    })

    it('loadGuilds resets to [] on non-ok response', async () => {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false }))
      const store = useGuildStore()
      store.guilds = [{ id: 'g-1' }]
      await store.loadGuilds()
      expect(store.guilds).toEqual([])
    })

    it('updateGuild patches currentGuild and guilds list locally', async () => {
      vi.stubGlobal(
        'fetch',
        vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
      )
      const store = useGuildStore()
      store.currentGuild = { id: 'g-1', name: 'old' }
      store.guilds = [{ id: 'g-1', name: 'old' }]

      await store.updateGuild('g-1', { name: 'new' })

      expect(store.currentGuild?.name).toBe('new')
      expect(store.guilds[0].name).toBe('new')
    })

    it('createGuild prepends the new guild to the list', async () => {
      vi.stubGlobal(
        'fetch',
        vi.fn().mockResolvedValue({
          ok: true,
          json: () => Promise.resolve({ id: 'g-new', name: 'fresh' }),
        }),
      )
      const store = useGuildStore()
      store.guilds = [{ id: 'g-old', name: 'old' }]

      const created = await store.createGuild('fresh')

      expect(created.id).toBe('g-new')
      expect(store.guilds[0].id).toBe('g-new')
      expect(store.guilds).toHaveLength(2)
    })

    it('joinGuild loads guild detail and seeds messages', async () => {
      const guild = { id: 'g-1', name: 'L', messages: [{ type: 'chat', from: 'u', content: 'h' }] }
      vi.stubGlobal(
        'fetch',
        vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(guild) }),
      )
      const store = useGuildStore()

      const result = await store.joinGuild('g-1')

      expect(result?.id).toBe('g-1')
      expect(store.currentGuild?.id).toBe('g-1')
      expect(store.messages).toHaveLength(1)
    })

    it('joinGuild returns null on failure', async () => {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false }))
      const store = useGuildStore()
      const result = await store.joinGuild('g-1')
      expect(result).toBeNull()
    })
  })
})
