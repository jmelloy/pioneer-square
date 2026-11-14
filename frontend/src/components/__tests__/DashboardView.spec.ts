import { beforeEach, describe, expect, it, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import DashboardView from '../dashboard/DashboardView.vue'
import { useGuildStore } from '../../stores/guild'
import { useConversationsStore } from '../../stores/conversations'
import { useTasksStore } from '../../stores/tasks'
import { useUiStore } from '../../stores/ui'
import { api } from '../../utils/api'
import type { Conversation, Task } from '../../types'

vi.mock('../../utils/api', async () => {
  const actual = await vi.importActual<typeof import('../../utils/api')>('../../utils/api')
  return { ...actual, api: vi.fn() }
})
vi.mock('../FactoryFloor.vue', () => ({ default: { template: '<div class="floor-stub" />' } }))

const conv = (id: number, status: Conversation['status'], updated_at: string): Conversation => ({
  id,
  user_id: null,
  discord_thread_id: null,
  name: `conv ${id}`,
  status,
  created_at: updated_at,
  updated_at,
})

describe('DashboardView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.mocked(api).mockReset()
  })

  it('headlines the newest active conversation and previews its messages', async () => {
    vi.mocked(api).mockResolvedValue([
      { type: 'chat', from: 'jmelloy', content: 'hello' },
      { type: 'chat', from: 'foreman', role: 'tool_use', content: 'hidden' },
    ])
    useGuildStore().currentGuild = { id: 'g1', name: 'Guild' }
    useConversationsStore().conversations = [
      conv(1, 'active', '2026-01-01T00:00:00Z'),
      conv(2, 'archived', '2026-03-01T00:00:00Z'),
      conv(3, 'active', '2026-02-01T00:00:00Z'),
    ]
    const wrapper = mount(DashboardView)
    await flushPromises()

    expect(wrapper.find('.focus-title').text()).toBe('conv 3')
    expect(vi.mocked(api)).toHaveBeenCalledWith('/api/guilds/g1/conversations/3/messages')
    expect(wrapper.text()).toContain('hello')
    expect(wrapper.text()).not.toContain('hidden')
  })

  it('previews 5 newest tasks, "View all" shows every task, rows open task tabs', async () => {
    useTasksStore().tasks = Array.from({ length: 7 }, (_, i) => ({
      id: `t${i}`,
      name: `task ${i}`,
      state: 'pending',
      created_at: `2026-01-0${i + 1}T00:00:00Z`,
    })) as Task[]
    const wrapper = mount(DashboardView)

    const rows = () => wrapper.findAll('.task-row')
    expect(rows()).toHaveLength(5)
    expect(rows()[0].text()).toContain('task 6')

    await wrapper
      .findAll('.link-btn')
      .find((b) => b.text().includes('View all'))!
      .trigger('click')
    expect(rows()).toHaveLength(7)

    await rows()[0].trigger('click')
    expect(useUiStore().activeTab).toBe('task-t6')
  })
})
