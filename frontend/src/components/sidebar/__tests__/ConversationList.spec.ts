import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import ConversationList from '../ConversationList.vue'
import { useGuildStore } from '../../../stores/guild'
import { useConversationsStore } from '../../../stores/conversations'
import { useUiStore } from '../../../stores/ui'
import { api } from '../../../utils/api'
import type { Conversation, TaskTreeData, TaskTreeNode } from '../../../types'

vi.mock('../../../utils/api', async () => {
  const actual = await vi.importActual<typeof import('../../../utils/api')>('../../../utils/api')
  return { ...actual, api: vi.fn() }
})

const conversations: Conversation[] = ['active', 'archived', 'closed'].map((status, index) => ({
  id: index + 1,
  user_id: 'u-1',
  discord_thread_id: null,
  name: ['Deploy pipeline', 'Fix login', 'Old deployment'][index],
  status: status as Conversation['status'],
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}))

const task = (id: string, state: string, children: TaskTreeNode[] = []): TaskTreeNode =>
  ({ id, name: `Task ${id}`, state, children }) as TaskTreeNode

const tree: TaskTreeData = {
  conversations: [
    { ...conversations[0], tasks: [task('t-parent', 'running', [task('t-child', 'done')])] },
  ],
  ungrouped: [task('t-loose', 'done')],
}

async function mountList() {
  useGuildStore().currentGuild = { id: 'g1', name: 'Guild 1' }
  vi.mocked(api).mockImplementation(async (path: string) =>
    path.endsWith('/tasks/tree')
      ? tree
      : conversations.map((conversation) => ({ ...conversation })),
  )
  const wrapper = mount(ConversationList)
  await flushPromises()
  return wrapper
}

describe('ConversationList', () => {
  beforeEach(() => setActivePinia(createPinia()))
  afterEach(() => {
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('defaults to active conversations while retaining other statuses in the store', async () => {
    const wrapper = await mountList()
    expect(wrapper.find('.filter-btn.active').text()).toBe('Active')
    expect(wrapper.findAll('.conversation-row')).toHaveLength(1)
    expect(wrapper.text()).toContain('Deploy pipeline')
    expect(wrapper.text()).not.toContain('Fix login')
    expect(wrapper.text()).not.toContain('Old deployment')
    expect(useConversationsStore().conversations).toEqual(conversations)
    wrapper.unmount()
  })

  it('filters only the sidebar without replacing tab data or changing selection', async () => {
    const wrapper = await mountList()
    const ui = useUiStore()
    const store = useConversationsStore()
    ui.selectConversation(1)
    ui.openConversationTab(2)
    const openedIds = [...ui.openedConversationIds]
    const selectedId = ui.selectedConversationId
    const activeTab = ui.activeTab

    for (const [index, count] of [
      [1, 1],
      [2, 1],
      [3, 1],
      [0, 3],
    ]) {
      await wrapper.findAll('.filter-btn')[index].trigger('click')
      expect(wrapper.findAll('.conversation-row')).toHaveLength(count)
      expect(store.conversations).toEqual(conversations)
      expect(ui.openedConversationIds).toEqual(openedIds)
      expect(ui.selectedConversationId).toBe(selectedId)
      expect(ui.activeTab).toBe(activeTab)
    }
    expect(api).toHaveBeenCalledTimes(2)
    expect(api).toHaveBeenCalledWith('/api/guilds/g1/conversations')
    expect(api).toHaveBeenCalledWith('/guilds/g1/tasks/tree')
    wrapper.unmount()
  })

  it('combines server search with status and live updates without replacing tab data', async () => {
    const wrapper = await mountList()
    await wrapper.findAll('.filter-btn')[0].trigger('click')
    vi.useFakeTimers()
    vi.mocked(api).mockResolvedValueOnce([conversations[0], conversations[2]])
    const search = wrapper.get('input[type="search"]')
    await search.setValue('  DEPLOY  ')
    await vi.advanceTimersByTimeAsync(250)
    expect(api).toHaveBeenLastCalledWith('/api/guilds/g1/conversations?q=DEPLOY')
    expect(useConversationsStore().conversations).toEqual(conversations)
    expect(wrapper.findAll('.conversation-row')).toHaveLength(2)
    await wrapper.findAll('.filter-btn')[1].trigger('click')
    expect(wrapper.findAll('.conversation-row')).toHaveLength(1)

    useConversationsStore().handleWebSocketMessage({
      type: 'conversation-updated',
      conversationId: 1,
      status: 'archived',
    })
    await flushPromises()
    expect(wrapper.findAll('.conversation-row')).toHaveLength(0)
    expect(wrapper.text()).toContain('No matching conversations.')
    await wrapper.findAll('.filter-btn')[2].trigger('click')
    expect(wrapper.findAll('.conversation-row')).toHaveLength(1)
    await search.setValue('')
    expect(wrapper.findAll('.conversation-row')).toHaveLength(2)
    wrapper.unmount()
  })

  it('shows message-only matches from older conversations without dropping open tabs', async () => {
    const wrapper = await mountList()
    vi.useFakeTimers()
    const ui = useUiStore()
    ui.selectConversation(1)
    ui.openConversationTab(1)
    const older = { ...conversations[0], id: 999, name: 'Unrelated title' }
    vi.mocked(api).mockResolvedValueOnce([older])
    await wrapper.get('input[type="search"]').setValue('message text')
    await vi.advanceTimersByTimeAsync(250)
    expect(wrapper.findAll('.conversation-row')).toHaveLength(1)
    expect(wrapper.text()).toContain('Unrelated title')
    expect(useConversationsStore().conversations).toEqual(conversations)
    expect(ui.openedConversationIds).toEqual([1])
    expect(ui.activeTab).toBe('conversation-1')
    wrapper.unmount()
  })

  it('ignores outdated search responses after clearing the search', async () => {
    const wrapper = await mountList()
    vi.useFakeTimers()
    let resolve!: (results: Conversation[]) => void
    vi.mocked(api).mockReturnValueOnce(
      new Promise<Conversation[]>((done) => {
        resolve = done
      }),
    )
    const search = wrapper.get('input[type="search"]')
    await search.setValue('needle')
    await vi.advanceTimersByTimeAsync(250)
    await search.setValue('')
    resolve([])
    await vi.advanceTimersByTimeAsync(0)
    expect(wrapper.findAll('.conversation-row')).toHaveLength(1)
    wrapper.unmount()
  })

  it('reports search failures rather than claiming there are no matches', async () => {
    const wrapper = await mountList()
    vi.useFakeTimers()
    vi.spyOn(console, 'error').mockImplementation(() => {})
    vi.mocked(api).mockRejectedValueOnce(new Error('unavailable'))
    await wrapper.get('input[type="search"]').setValue('needle')
    await vi.advanceTimersByTimeAsync(250)
    expect(wrapper.get('[role="alert"]').text()).toContain('Search failed')
    wrapper.unmount()
  })

  it('nests the task hierarchy under its conversation with expand/collapse and open', async () => {
    const wrapper = await mountList()
    await wrapper.findAll('.filter-btn')[0].trigger('click')
    const row = wrapper.findAll('.conversation-row')[0]
    // Live work → expanded by default; parent task's own toggle reveals the child.
    expect(wrapper.text()).toContain('Task t-parent')
    expect(wrapper.text()).toContain('Task t-loose')
    await row.trigger('click')
    expect(wrapper.text()).not.toContain('Task t-parent')
    expect(useUiStore().selectedConversationId).toBeNull()

    await wrapper.findAll('.open-btn')[0].trigger('click')
    expect(useUiStore().selectedConversationId).toBe(1)
    wrapper.unmount()
  })

  it('filters by time range and caps to the last N', async () => {
    const recent = new Date().toISOString()
    conversations[0].updated_at = recent
    const wrapper = await mountList()
    conversations[0].updated_at = '2026-01-01T00:00:00Z'
    const [range, limit] = wrapper.findAll('select')
    await range.setValue('24')
    expect(wrapper.findAll('.conversation-row')).toHaveLength(1)
    await range.setValue('0')
    await wrapper.findAll('.filter-btn')[0].trigger('click')
    useConversationsStore().conversations = Array.from({ length: 30 }, (_, i) => ({
      ...conversations[1],
      id: 100 + i,
    }))
    await flushPromises()
    expect(wrapper.findAll('.conversation-row')).toHaveLength(25)
    await limit.setValue('0')
    expect(wrapper.findAll('.conversation-row')).toHaveLength(30)
    wrapper.unmount()
  })
})
