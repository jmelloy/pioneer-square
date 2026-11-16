import { beforeEach, describe, expect, it, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useTasksStore } from '../tasks'
import { useUiStore } from '../ui'

describe('useTasksStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  describe('handleWebSocketMessage', () => {
    it('inserts a new task on task-created', () => {
      const store = useTasksStore()
      store.handleWebSocketMessage({
        type: 'task-created',
        taskId: 't-1',
        name: 'Fix bug',
        description: 'Investigate flaky test',
        phase: 'execute',
        state: 'pending',
        createdAt: '2025-01-01T00:00:00Z',
      })

      expect(store.tasks).toHaveLength(1)
      expect(store.tasks[0]).toMatchObject({
        id: 't-1',
        name: 'Fix bug',
        state: 'pending',
        worker_id: null,
      })
    })

    it.each([
      { type: 'task-created', taskId: 't-linked', state: 'pending', conversationId: 42 },
      { type: 'task-assigned', taskId: 't-linked', workerId: 'w-abc', conversationId: 42 },
    ] as const)('keeps the conversation link from $type', (event) => {
      const store = useTasksStore()
      store.handleWebSocketMessage(event)

      expect(store.tasks[0].conversation_id).toBe(42)
    })

    it('preserves the conversation link when an assignment omits it', () => {
      const store = useTasksStore()
      store.tasks.push({ id: 't-1', state: 'pending', conversation_id: 42 })

      store.handleWebSocketMessage({
        type: 'task-assigned',
        taskId: 't-1',
        workerId: 'w-abc',
      })

      expect(store.tasks[0].conversation_id).toBe(42)
    })

    it('upserts an existing task on task-assigned without resetting created_at', () => {
      const store = useTasksStore()
      store.tasks.push({
        id: 't-1',
        state: 'pending',
        created_at: '2025-01-01T00:00:00Z',
      })

      store.handleWebSocketMessage({
        type: 'task-assigned',
        taskId: 't-1',
        description: 'now with worker',
        workerId: 'w-abc',
      })

      expect(store.tasks).toHaveLength(1)
      expect(store.tasks[0].worker_id).toBe('w-abc')
      expect(store.tasks[0].created_at).toBe('2025-01-01T00:00:00Z')
    })

    it('derives a name from description when task-assigned omits name', () => {
      const store = useTasksStore()
      const longDescription = 'a'.repeat(120)

      store.handleWebSocketMessage({
        type: 'task-assigned',
        taskId: 't-2',
        description: longDescription,
        workerId: 'w-abc',
      })

      expect(store.tasks[0].name).toBe('a'.repeat(60))
    })

    it('updates state, branch, prUrl, worktreePath on task-update', () => {
      const store = useTasksStore()
      store.tasks.push({ id: 't-1', state: 'pending' })

      store.handleWebSocketMessage({
        type: 'task-update',
        taskId: 't-1',
        state: 'working',
        branch: 'claude/fix-1',
        prUrl: 'https://github.com/x/y/pull/1',
        worktreePath: '/tmp/wt',
      })

      const task = store.tasks[0]
      expect(task.state).toBe('working')
      expect(task.branch).toBe('claude/fix-1')
      expect(task.pr_url).toBe('https://github.com/x/y/pull/1')
      expect(task.worktree_path).toBe('/tmp/wt')
    })

    it('moves task to awaiting-review on task-complete', () => {
      const store = useTasksStore()
      store.tasks.push({ id: 't-1', state: 'working' })

      store.handleWebSocketMessage({
        type: 'task-complete',
        taskId: 't-1',
        branch: 'claude/done',
      })

      expect(store.tasks[0].state).toBe('awaiting-review')
      expect(store.tasks[0].branch).toBe('claude/done')
    })

    it('moves task to awaiting-review on task-followup-done', () => {
      const store = useTasksStore()
      store.tasks.push({ id: 't-1', state: 'followup' })

      store.handleWebSocketMessage({ type: 'task-followup-done', taskId: 't-1' })

      expect(store.tasks[0].state).toBe('awaiting-review')
    })

    it('does not override cancelled state on task-complete (race condition guard)', () => {
      const store = useTasksStore()
      store.tasks.push({ id: 't-1', state: 'cancelled' })

      store.handleWebSocketMessage({
        type: 'task-complete',
        taskId: 't-1',
        branch: 'claude/done',
      })

      expect(store.tasks[0].state).toBe('cancelled')
    })

    it('does not override cancelled state on task-followup-done', () => {
      const store = useTasksStore()
      store.tasks.push({ id: 't-1', state: 'cancelled' })

      store.handleWebSocketMessage({ type: 'task-followup-done', taskId: 't-1' })

      expect(store.tasks[0].state).toBe('cancelled')
    })

    it('appends terminal-output lines into taskLogs', () => {
      const store = useTasksStore()

      store.handleWebSocketMessage({
        type: 'terminal-output',
        taskId: 't-1',
        line: 'hello',
        timestamp: '2025-01-01T00:00:00Z',
      })

      expect(store.taskLogs['t-1']).toHaveLength(1)
      expect(store.taskLogs['t-1'][0].line).toBe('hello')
    })

    it('caps task logs at 2000 entries', () => {
      const store = useTasksStore()
      for (let i = 0; i < 2050; i++) {
        store.handleWebSocketMessage({
          type: 'terminal-output',
          taskId: 't-1',
          line: `line ${i}`,
          timestamp: '2025-01-01T00:00:00Z',
        })
      }
      expect(store.taskLogs['t-1'].length).toBe(2000)
      // Oldest entries are dropped from the front.
      expect(store.taskLogs['t-1'][0].line).toBe('line 50')
    })

    it('ignores terminal-output with no taskId', () => {
      const store = useTasksStore()
      store.handleWebSocketMessage({
        type: 'terminal-output',
        line: 'orphan',
        timestamp: '2025-01-01T00:00:00Z',
      })
      expect(Object.keys(store.taskLogs)).toHaveLength(0)
    })

    it('is a no-op for unrelated message types', () => {
      const store = useTasksStore()
      // Partial payload — handler should ignore unrelated types regardless of shape.
      store.handleWebSocketMessage({ type: 'agent-state', agentId: 'a-1', state: 'idle' })
      expect(store.tasks).toHaveLength(0)
    })
  })

  describe('finishedAt', () => {
    it('removes the row when a task-update carries finishedAt', () => {
      const store = useTasksStore()
      store.tasks.push({ id: 't-1', state: 'working' })
      store.handleWebSocketMessage({
        type: 'task-update',
        taskId: 't-1',
        state: 'done',
        finishedAt: new Date().toISOString(),
      })
      expect(store.tasks).toHaveLength(0)
    })

    it('keeps the row when finishedAt is null (done task on an open issue)', () => {
      const store = useTasksStore()
      store.tasks.push({ id: 't-1', state: 'working' })
      store.handleWebSocketMessage({
        type: 'task-update',
        taskId: 't-1',
        state: 'done',
        finishedAt: null,
      })
      expect(store.tasks[0].state).toBe('done')
    })

    it('liveTasks excludes rows with finished_at', () => {
      const store = useTasksStore()
      store.tasks.push({ id: 't-live', state: 'done' })
      store.tasks.push({ id: 't-done', state: 'done', finished_at: new Date().toISOString() })
      expect(store.liveTasks.map((t) => t.id)).toEqual(['t-live'])
    })
  })

  // Selection/tab state (selectTask/closeTask/selectedTaskId/openedTaskIds) now
  // lives in the ui store — see ui.spec.ts. clearTasks still delegates to it.
  describe('clearTasks', () => {
    it('resets tasks, taskLogs, and delegates selection reset to the ui store', () => {
      const store = useTasksStore()
      const uiStore = useUiStore()
      store.tasks.push({ id: 't-1', state: 'pending' })
      store.taskLogs['t-1'] = [{ line: 'x', timestamp: '', detail: null }]
      uiStore.selectTask('t-1')

      store.clearTasks()

      expect(store.tasks).toEqual([])
      expect(store.taskLogs).toEqual({})
      expect(uiStore.selectedTaskId).toBeNull()
      expect(uiStore.openedTaskIds).toEqual([])
    })
  })

  describe('stateLabel / stateColor', () => {
    it('returns mapped labels and colors for known states', () => {
      const store = useTasksStore()
      expect(store.stateLabel('awaiting-review')).toBe('review')
      expect(store.stateColor('awaiting-review')).toBe('amber')
      expect(store.stateLabel('done')).toBe('done')
      expect(store.stateColor('done')).toBe('teal')
    })

    it('falls back to the raw state / dim for unknown values', () => {
      const store = useTasksStore()
      expect(store.stateLabel('mystery' as any)).toBe('mystery')
      expect(store.stateColor('mystery' as any)).toBe('dim')
    })
  })

  describe('REST helpers', () => {
    beforeEach(() => {
      // happy-dom has fetch; replace it per test.
      vi.stubGlobal(
        'fetch',
        vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve([]) }),
      )
    })

    it('fetchTasks populates tasks on success', async () => {
      const fakeTasks = [{ id: 't-1', state: 'pending' }]
      vi.stubGlobal(
        'fetch',
        vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(fakeTasks) }),
      )
      const store = useTasksStore()
      await store.fetchTasks('g-1')
      expect(store.tasks).toEqual(fakeTasks)
      expect(fetch).toHaveBeenCalledWith(
        '/guilds/g-1/tasks',
        expect.objectContaining({ headers: expect.any(Object) }),
      )
    })

    it('fetchTasks short-circuits on empty guildId', async () => {
      const store = useTasksStore()
      await store.fetchTasks('')
      expect(fetch).not.toHaveBeenCalled()
    })

    it.each([
      ['sendFollowup', 'followup', { instructions: 'do X' }],
      ['redirectTask', 'redirect', { instructions: 'do Y' }],
    ] as const)('%s POSTs to the right URL with JSON body', async (method, path, body) => {
      const store = useTasksStore()
      vi.stubGlobal(
        'fetch',
        vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
      )
      await (store as any)[method]('g-1', 't-1', body.instructions)
      expect(fetch).toHaveBeenCalledWith(
        `/guilds/g-1/tasks/t-1/${path}`,
        expect.objectContaining({
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        }),
      )
    })

    it('finalizeTask and cancelTask POST without a body', async () => {
      const store = useTasksStore()
      const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) })
      vi.stubGlobal('fetch', fetchMock)

      await store.finalizeTask('g-1', 't-1')
      await store.cancelTask('g-1', 't-1')

      expect(fetchMock).toHaveBeenNthCalledWith(
        1,
        '/guilds/g-1/tasks/t-1/finalize',
        expect.objectContaining({
          method: 'POST',
        }),
      )
      expect(fetchMock).toHaveBeenNthCalledWith(
        2,
        '/guilds/g-1/tasks/t-1/cancel',
        expect.objectContaining({
          method: 'POST',
        }),
      )
    })

    it('cancelTask optimistically marks the task cancelled before API resolves', async () => {
      const store = useTasksStore()
      store.tasks.push({ id: 't-1', state: 'working' })
      let resolveApi!: () => void
      vi.stubGlobal(
        'fetch',
        vi.fn().mockReturnValue(
          new Promise<{ ok: boolean; json: () => Promise<object> }>((res) => {
            resolveApi = () => res({ ok: true, json: () => Promise.resolve({}) })
          }),
        ),
      )

      const promise = store.cancelTask('g-1', 't-1')
      // State must be updated before the API call resolves
      expect(store.tasks[0].state).toBe('cancelled')
      expect(store.tasks[0].finished_at).toBeDefined()

      resolveApi()
      await promise
      expect(store.tasks[0].state).toBe('cancelled')
    })

    it('cancelTask rolls back state and re-throws on API failure', async () => {
      const store = useTasksStore()
      store.tasks.push({ id: 't-1', state: 'working' })
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 500 }))

      await expect(store.cancelTask('g-1', 't-1')).rejects.toThrow('HTTP 500')
      expect(store.tasks[0].state).toBe('working')
      expect(store.tasks[0].finished_at).toBeUndefined()
    })

    it('throws when the API returns non-2xx', async () => {
      const store = useTasksStore()
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 500 }))
      await expect(store.finalizeTask('g-1', 't-1')).rejects.toThrow('HTTP 500')
    })
  })
})
