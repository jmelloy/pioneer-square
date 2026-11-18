import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { api } from '../../utils/api'
import { useAgentsStore } from '../agents'
import { useTasksStore } from '../tasks'
import { useUsageStore } from '../usage'
import { useGuildStore } from '../guild'
import { useGitHubStore } from '../github'

vi.mock('../../utils/api', () => ({ api: vi.fn() }))
const apiMock = vi.mocked(api)

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

describe('guild switch request isolation', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    apiMock.mockReset()
  })
  afterEach(() => vi.unstubAllGlobals())

  it.each([
    {
      name: 'tasks',
      start: () => useTasksStore().fetchTasks('old'),
      clear: () => useTasksStore().clearTasks(),
      read: () => useTasksStore().tasks,
      response: [{ id: 'old-task', state: 'working' }],
      empty: [],
    },
    {
      name: 'task logs',
      start: () => useTasksStore().fetchTaskLogs('old', 'old-task'),
      clear: () => useTasksStore().clearTasks(),
      read: () => useTasksStore().taskLogs,
      response: [{ line: 'old log', timestamp: '2026-01-01T00:00:00Z' }],
      empty: {},
    },
    {
      name: 'worker logs',
      start: () => useAgentsStore().fetchWorkerLogs('old', 'old-worker'),
      clear: () => useAgentsStore().clearAgents(),
      read: () => useAgentsStore().workerLogs,
      response: [{ line: 'old log', timestamp: '2026-01-01T00:00:00Z' }],
      empty: {},
    },
    {
      name: 'usage',
      start: () => useUsageStore().fetchUsage('old'),
      clear: () => useUsageStore().clearUsage(),
      read: () => useUsageStore().byTask,
      response: [{ task_id: 'old-task', kind: 'result', input_tokens: 100, output_tokens: 10 }],
      empty: {},
    },
  ])('ignores pending $name after reset', async ({ start, clear, read, response, empty }) => {
    const pending = deferred<unknown>()
    apiMock.mockReturnValueOnce(pending.promise)
    const request = start()
    clear()
    pending.resolve(response)
    await request
    expect(read()).toEqual(empty)
  })

  it('does not let an older join overwrite the latest guild and messages', async () => {
    const store = useGuildStore()
    const pending = deferred<unknown>()
    apiMock.mockReturnValueOnce(pending.promise)
    const oldJoin = store.joinGuild('old')
    apiMock.mockResolvedValueOnce({ id: 'new', messages: [] })
    await store.joinGuild('new')
    pending.resolve({ id: 'old', messages: [{ from: 'user', content: 'old chat' }] })
    expect(await oldJoin).toBeNull()
    expect(store.currentGuild?.id).toBe('new')
    expect(store.messages).toEqual([])
  })

  it('clears the guild, messages and poll timing and invalidates a pending join', async () => {
    const store = useGuildStore()
    store.currentGuild = { id: 'old' }
    store.messages = [{ type: 'chat', from: 'user', content: 'old chat' }]
    store.nextPollAt = Date.now()
    const pending = deferred<unknown>()
    apiMock.mockReturnValueOnce(pending.promise)
    const request = store.joinGuild('old')
    store.clearGuild()
    expect(store.currentGuild).toBeNull()
    expect(store.messages).toEqual([])
    expect(store.nextPollAt).toBeNull()
    pending.resolve({ id: 'old' })
    expect(await request).toBeNull()
    expect(store.currentGuild).toBeNull()
  })

  it('clears issues and tabs, ignores pending issues, and keeps account preferences', async () => {
    const store = useGitHubStore()
    store.token = 'account-token'
    store.openIssueTab('owner', 'repo', 1)
    store.error = 'old error'
    const pending = deferred<Response>()
    vi.stubGlobal('fetch', vi.fn().mockReturnValueOnce(pending.promise))
    const request = store.fetchIssues(['owner/repo'])
    store.clearIssues()
    expect(store.openedIssueKeys).toEqual([])
    expect(store.loading).toBe(false)
    expect(store.error).toBe('')
    pending.resolve(Response.json([{ number: 1, labels: [], created_at: '2026-01-01' }]))
    await request
    expect(store.issues).toEqual([])
    expect(store.token).toBe('account-token')
  })
})
