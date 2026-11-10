import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import ConversationDetailPanel from '../ConversationDetailPanel.vue'
import { useGuildStore } from '../../stores/guild'
import { useConversationsStore } from '../../stores/conversations'
import { useUiStore } from '../../stores/ui'
import { api, ApiError } from '../../utils/api'

vi.mock('../../utils/api', async () => {
  const actual = await vi.importActual<typeof import('../../utils/api')>('../../utils/api')
  return { ...actual, api: vi.fn() }
})

const apiMock = vi.mocked(api)

const CONVERSATION = {
  id: 42,
  user_id: 'u-1',
  discord_thread_id: null,
  name: 'Deploy pipeline',
  status: 'active' as const,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

function mountPanel() {
  const guildStore = useGuildStore()
  guildStore.currentGuild = { id: 'g1', name: 'Guild 1' }
  const conversationsStore = useConversationsStore()
  conversationsStore.conversations = [{ ...CONVERSATION }]

  apiMock.mockResolvedValueOnce([]) // fetchConversationMessages on mount
  apiMock.mockResolvedValueOnce([]) // fetchTasks on mount

  const wrapper = mount(ConversationDetailPanel, { props: { id: 42 } })
  return { wrapper, guildStore, conversationsStore }
}

describe('ConversationDetailPanel reply', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('disables send while input is empty', async () => {
    const { wrapper } = mountPanel()
    await flushPromises()
    const button = wrapper.find('.send-btn')
    expect((button.element as HTMLButtonElement).disabled).toBe(true)
  })

  it('posts the reply and appends it to the thread on success', async () => {
    const { wrapper } = mountPanel()
    await flushPromises()

    apiMock.mockResolvedValueOnce({
      id: 7,
      conversationId: 42,
      content: 'sounds good',
      createdAt: '2026-01-02T00:00:00Z',
    })

    await wrapper.find('.reply-input').setValue('sounds good')
    await wrapper.find('.send-btn').trigger('click')
    await flushPromises()

    expect(apiMock).toHaveBeenLastCalledWith(
      '/api/guilds/g1/conversations/42/messages',
      expect.objectContaining({ method: 'POST', json: { content: 'sounds good' } }),
    )
    expect(wrapper.text()).toContain('sounds good')
    expect((wrapper.find('.reply-input').element as HTMLTextAreaElement).value).toBe('')
  })

  it('shows an error message when posting fails', async () => {
    const { wrapper } = mountPanel()
    await flushPromises()

    apiMock.mockRejectedValueOnce(new ApiError('Too many messages — please slow down', 429))

    await wrapper.find('.reply-input').setValue('spam')
    await wrapper.find('.send-btn').trigger('click')
    await flushPromises()

    expect(wrapper.find('.reply-error').text()).toBe('Too many messages — please slow down')
  })

  it('does not show the reply input for closed conversations', async () => {
    const guildStore = useGuildStore()
    guildStore.currentGuild = { id: 'g1', name: 'Guild 1' }
    const conversationsStore = useConversationsStore()
    conversationsStore.conversations = [{ ...CONVERSATION, status: 'closed' }]
    apiMock.mockResolvedValueOnce([])
    apiMock.mockResolvedValueOnce([])

    const wrapper = mount(ConversationDetailPanel, { props: { id: 42 } })
    await flushPromises()

    expect(wrapper.find('.reply-row').exists()).toBe(false)
  })
})

describe('ConversationDetailPanel linked task', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('shows name, status and PR link for tasks linked to the conversation', async () => {
    const guildStore = useGuildStore()
    guildStore.currentGuild = { id: 'g1', name: 'Guild 1' }
    useConversationsStore().conversations = [{ ...CONVERSATION }]
    apiMock.mockResolvedValueOnce([])
    apiMock.mockResolvedValueOnce([
      {
        id: 't1',
        name: 'Fix bug',
        state: 'working',
        pr_url: 'https://x/pr/1',
        conversation_id: 42,
      },
      { id: 't2', name: 'Other', state: 'done', conversation_id: 7 },
    ])
    const wrapper = mount(ConversationDetailPanel, { props: { id: 42 } })
    await flushPromises()
    const text = wrapper.find('.pane-tasks').text()
    expect(text).toContain('Fix bug')
    expect(text).toContain('working')
    expect(text).not.toContain('Other')
    expect(wrapper.find('.linked-task-pr').attributes('href')).toBe('https://x/pr/1')
  })

  it('interleaves task output, PR checks and replies without crossing conversations', async () => {
    const { wrapper, guildStore } = mountPanel()
    await flushPromises()
    const ui = useUiStore()
    const openTask = vi.spyOn(ui, 'openTaskTab')
    for (const message of [
      { from: 'a-1', source: 'worker', taskId: 't1', content: '▶ Running <tests>\nnext line' },
      {
        from: 'github',
        source: 'github',
        taskId: 't2',
        content: '**pytest: success**\n\n42 passed',
      },
      { from: 'user', source: 'web', content: 'Please focus on lint next' },
    ]) {
      guildStore.handleWebSocketMessage({
        type: 'chat',
        conversationId: 42,
        createdAt: '2026-01-02T00:00:00Z',
        ...message,
      })
    }
    guildStore.handleWebSocketMessage({
      type: 'chat',
      from: 'a-2',
      conversationId: 99,
      content: 'Unrelated work',
    })
    await flushPromises()
    expect(wrapper.findAll('.chat-message')).toHaveLength(3)
    expect(wrapper.find('.from-worker .log-text').text()).toBe('▶ Running <tests>\nnext line')
    expect(wrapper.find('.from-github').exists()).toBe(true)
    expect(wrapper.find('.msg-content--markdown strong').text()).toBe('pytest: success')
    expect(wrapper.text()).not.toContain('Unrelated work')
    await wrapper.findAll('.msg-task-badge')[1].trigger('click')
    expect(openTask).toHaveBeenCalledWith('t2')
  })

  it('renders worker tool details and links like the task pane without repeated headers', async () => {
    const { wrapper, guildStore } = mountPanel()
    await flushPromises()
    for (const message of [
      {
        content: '▶ bash: run tests',
        detail: { toolType: 'tool_use' as const, name: 'bash', input: { command: 'npm test' } },
      },
      { content: 'PR: https://github.com/org/repo/pull/1', level: 'worker' as const },
    ]) {
      guildStore.handleWebSocketMessage({
        type: 'chat',
        from: 'a-1',
        source: 'worker',
        taskId: 't1',
        conversationId: 42,
        createdAt: '2026-01-02T00:00:00Z',
        ...message,
      })
    }
    await flushPromises()
    expect(wrapper.findAll('.from-worker .msg-header')).toHaveLength(1)
    expect(wrapper.findAll('.worker-continuation')).toHaveLength(1)
    await wrapper.find('.log-line--expandable').trigger('click')
    expect(wrapper.find('.log-detail-body').text()).toBe('npm test')
    expect(wrapper.find('a.terminal-link').attributes('href')).toBe(
      'https://github.com/org/repo/pull/1',
    )
    await wrapper.find('.log-line--expandable').trigger('click')
    expect(wrapper.find('.log-detail').exists()).toBe(false)
    guildStore.handleWebSocketMessage({
      type: 'chat',
      from: 'a-1',
      source: 'worker',
      taskId: 't2',
      conversationId: 42,
      content: 'Other task',
    })
    await flushPromises()
    expect(wrapper.findAll('.from-worker .msg-header')).toHaveLength(2)
  })

  it('loads earlier activity before the current page', async () => {
    const guildStore = useGuildStore()
    guildStore.currentGuild = { id: 'g1', name: 'Guild 1' }
    useConversationsStore().conversations = [{ ...CONVERSATION }]
    apiMock.mockResolvedValueOnce(
      Array.from({ length: 100 }, (_, i) => ({
        id: i + 2,
        from_agent: 'worker',
        content: `Output ${i}`,
        conversationId: 42,
        created_at: '2026-01-02T00:00:00Z',
        source: 'worker',
      })),
    )
    apiMock.mockResolvedValueOnce([])
    const wrapper = mount(ConversationDetailPanel, { props: { id: 42 } })
    await flushPromises()
    apiMock.mockResolvedValueOnce([
      {
        id: 1,
        from_agent: 'user',
        content: 'Original request',
        created_at: '2026-01-01T00:00:00Z',
      },
    ])
    const button = wrapper.find('.body button')
    expect(button.text()).toBe('Load earlier activity')
    await button.trigger('click')
    await flushPromises()
    expect(apiMock).toHaveBeenLastCalledWith('/api/guilds/g1/conversations/42/messages?before_id=2')
    expect(wrapper.findAll('.chat-message')[0].text()).toContain('Original request')
    expect(wrapper.find('.body button').exists()).toBe(false)
  })

  it('shows an error note when tasks fail to load', async () => {
    const guildStore = useGuildStore()
    guildStore.currentGuild = { id: 'g1', name: 'Guild 1' }
    useConversationsStore().conversations = [{ ...CONVERSATION }]
    apiMock.mockResolvedValueOnce([])
    apiMock.mockRejectedValueOnce(new Error('boom'))
    const wrapper = mount(ConversationDetailPanel, { props: { id: 42 } })
    await flushPromises()
    expect(wrapper.find('.pane-tasks-error').exists()).toBe(true)
  })
})
