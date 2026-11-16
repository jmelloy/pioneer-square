import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, shallowMount } from '@vue/test-utils'
import AppView from '../AppView.vue'
import { api } from '../../utils/api'
import { useAuthStore } from '../../stores/auth'
import { useGuildStore } from '../../stores/guild'
import { useAgentsStore } from '../../stores/agents'
import { useTasksStore } from '../../stores/tasks'
import { useConversationsStore } from '../../stores/conversations'
import { useUsageStore } from '../../stores/usage'
import { useGitHubStore } from '../../stores/github'
import { useUiStore } from '../../stores/ui'

vi.mock('../../utils/api', () => ({ api: vi.fn() }))
vi.mock('../../router', () => ({ getRootOrigin: () => null }))
vi.mock('../../composables/useQueryRef', () => ({ useQueryRef: vi.fn() }))
const replace = vi.hoisted(() => vi.fn())
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {} }),
  useRouter: () => ({ replace }),
}))

const apiMock = vi.mocked(api)
const sockets: FakeWebSocket[] = []
class FakeWebSocket {
  close = vi.fn()
  onmessage: ((event: MessageEvent) => void) | null = null
  constructor(public url: string) {
    sockets.push(this)
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

function mountView() {
  return shallowMount(AppView, {
    props: { guildId: 'old' },
    global: { stubs: { teleport: true } },
  })
}

describe('AppView guild lifecycle', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    replace.mockReset()
    apiMock.mockReset()
    sockets.length = 0
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const auth = useAuthStore()
    auth.loginToken = 'login-token'
    auth.user = { login: 'test', name: 'Test', avatar_url: '' }
    apiMock.mockImplementation(async (path) => {
      if (path === '/guilds/old') return { id: 'old', messages: [] }
      return []
    })
  })
  afterEach(() => vi.unstubAllGlobals())

  it('clears guild state immediately, disconnects the old socket and preserves login', async () => {
    const wrapper = mountView()
    await flushPromises()
    const guild = useGuildStore()
    const tasks = useTasksStore()
    const agents = useAgentsStore()
    const conversations = useConversationsStore()
    const usage = useUsageStore()
    const github = useGitHubStore()
    const ui = useUiStore()
    const oldSocket = sockets[0]
    const oldHandler = oldSocket.onmessage!
    guild.messages = [{ type: 'chat', from: 'user', content: 'old chat' }]
    guild.nextPollAt = Date.now()
    agents.registerAgent({ agentId: 'old-agent', agentType: 'worker', workerId: 'old-worker' })
    agents.workerLogs['old-worker'] = [
      { line: 'old worker log', timestamp: '2026-01-01T00:00:00Z' },
    ]
    tasks.handleWebSocketMessage({ type: 'task-created', taskId: 'old-task', state: 'working' })
    tasks.taskLogs['old-task'] = [{ line: 'old task log', timestamp: '2026-01-01T00:00:00Z' }]
    conversations.handleWebSocketMessage({ type: 'conversation-created', conversationId: 701 })
    usage.byTask['old-task'] = { taskId: 'old-task' } as (typeof usage.byTask)[string]
    ui.selectWorker('old-worker')
    ui.selectAgent('old-agent')
    ui.openTaskTab('old-task')
    ui.openConversationTab(701)
    github.openIssueTab('owner', 'repo', 1)
    const pending = deferred<unknown>()
    apiMock.mockImplementation(async (path) => (path === '/guilds/new' ? pending.promise : []))

    await wrapper.setProps({ guildId: 'new' })
    expect(oldSocket.close).toHaveBeenCalledOnce()
    expect(guild.currentGuild).toBeNull()
    expect(guild.messages).toEqual([])
    expect(guild.nextPollAt).toBeNull()
    expect(agents.agents).toEqual([])
    expect(agents.workerLogs).toEqual({})
    expect(tasks.tasks).toEqual([])
    expect(tasks.taskLogs).toEqual({})
    expect(conversations.conversations).toEqual([])
    expect(usage.byTask).toEqual({})
    expect(ui.openedWorkerIds).toEqual([])
    expect(ui.openedAgentIds).toEqual([])
    expect(ui.openedTaskIds).toEqual([])
    expect(ui.openedConversationIds).toEqual([])
    expect(ui.activeTab).toBe('factory')
    expect(github.openedIssueKeys).toEqual([])
    expect(useAuthStore().loginToken).toBe('login-token')
    oldHandler(
      new MessageEvent('message', {
        data: JSON.stringify({
          type: 'conversation-created',
          conversationId: 701,
        }),
      }),
    )
    expect(conversations.conversations).toEqual([])

    pending.resolve({ id: 'new', messages: [] })
    await flushPromises()
    expect(guild.currentGuild?.id).toBe('new')
    expect(sockets).toHaveLength(2)
    wrapper.unmount()
    expect(sockets[1].close).toHaveBeenCalledOnce()
    expect(guild.currentGuild).toBeNull()
  })

  it('does not reconnect or redirect when an older guild join finishes late', async () => {
    const pending = deferred<unknown>()
    apiMock.mockImplementation(async (path) => {
      if (path === '/guilds/old') return pending.promise
      if (path === '/guilds/new') return { id: 'new', messages: [] }
      return []
    })
    const wrapper = mountView()
    await wrapper.setProps({ guildId: 'new' })
    await flushPromises()
    pending.resolve({ id: 'old', messages: [] })
    await flushPromises()
    expect(useGuildStore().currentGuild?.id).toBe('new')
    expect(sockets).toHaveLength(1)
    expect(sockets[0].url).toContain('/ws/new')
    expect(replace).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('does not reconnect when an older task list finishes after a switch', async () => {
    const pending = deferred<unknown>()
    apiMock.mockImplementation(async (path) => {
      if (path === '/guilds/old') return { id: 'old', messages: [] }
      if (path === '/guilds/old/tasks') return pending.promise
      if (path === '/guilds/new') return { id: 'new', messages: [] }
      return []
    })
    const wrapper = mountView()
    await flushPromises()
    await wrapper.setProps({ guildId: 'new' })
    await flushPromises()
    pending.resolve([{ id: 'old-task', state: 'working' }])
    await flushPromises()
    expect(useTasksStore().tasks).toEqual([])
    expect(sockets).toHaveLength(1)
    expect(sockets[0].url).toContain('/ws/new')
    wrapper.unmount()
  })
})
