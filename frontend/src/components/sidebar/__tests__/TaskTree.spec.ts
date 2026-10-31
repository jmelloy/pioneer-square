import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import TaskTree from '../TaskTree.vue'
import { useGuildStore } from '../../../stores/guild'
import { useUiStore } from '../../../stores/ui'
import { api } from '../../../utils/api'

vi.mock('../../../utils/api', async () => {
  const actual = await vi.importActual<typeof import('../../../utils/api')>('../../../utils/api')
  return { ...actual, api: vi.fn() }
})

const RowStub = { props: ['task'], template: '<div class="row-stub">{{ task.id }}</div>' }

function task(id: number, state = 'running', children: unknown[] = []) {
  return { id, state, children }
}

function conv(id: number, name: string | null, status: string, tasks: unknown[]) {
  return {
    id,
    name,
    status,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: `2026-01-0${id}T00:00:00Z`,
    tasks,
  }
}

async function mountTree(data: unknown) {
  useGuildStore().currentGuild = { id: 'g1', name: 'Guild 1' }
  vi.mocked(api).mockResolvedValue(data)
  const wrapper = mount(TaskTree, { global: { stubs: { TaskTreeRow: RowStub } } })
  await flushPromises()
  return wrapper
}

describe('TaskTree', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
  })
  afterEach(() => vi.restoreAllMocks())

  it('shows empty state', async () => {
    const w = await mountTree({ conversations: [], ungrouped: [] })
    expect(w.text()).toContain('No tasks yet')
  })

  it('renders conversation headers with nested tasks and counts', async () => {
    const w = await mountTree({
      conversations: [conv(1, 'Deploy', 'active', [task(10, 'running', [task(11)])]), conv(2, null, 'archived', [task(20)])],
      ungrouped: [],
    })
    const text = w.text()
    expect(text).toContain('Deploy')
    expect(text).toContain('Unnamed conversation')
    expect(w.find('.badge-active').exists()).toBe(true)
    expect(w.find('.badge-archived').exists()).toBe(true)
    expect(w.findAll('.group-count')[0].text()).toBe('2')
  })

  it('hides conversations with only terminal tasks', async () => {
    const w = await mountTree({
      conversations: [conv(1, 'Done one', 'closed', [task(1, 'done')])],
      ungrouped: [task(5)],
    })
    expect(w.text()).not.toContain('Done one')
    expect(w.text()).toContain('Ungrouped')
  })

  it('toggles expand/collapse and persists conv:<id> key', async () => {
    const w = await mountTree({ conversations: [conv(1, 'Deploy', 'active', [task(10)])], ungrouped: [] })
    expect(w.findAll('.row-stub')).toHaveLength(1)
    await w.find('.group-header').trigger('click')
    expect(w.findAll('.row-stub')).toHaveLength(0)
    expect(JSON.parse(localStorage.getItem('pioneer_tree_expanded')!)).toContain('__collapsed__conv:1')
    await w.find('.group-header').trigger('click')
    expect(w.findAll('.row-stub')).toHaveLength(1)
  })

  it('open button selects conversation without toggling', async () => {
    const w = await mountTree({ conversations: [conv(7, 'X', 'active', [task(10)])], ungrouped: [] })
    await w.find('.open-btn').trigger('click')
    expect(useUiStore().selectedConversationId).toBe(7)
    expect(w.findAll('.row-stub')).toHaveLength(1)
  })

  it('renders and collapses ungrouped section', async () => {
    const w = await mountTree({ conversations: [], ungrouped: [task(5), task(6)] })
    expect(w.findAll('.row-stub')).toHaveLength(2)
    await w.find('.group-header').trigger('click')
    expect(w.findAll('.row-stub')).toHaveLength(0)
    expect(JSON.parse(localStorage.getItem('pioneer_tree_expanded')!)).toContain('__collapsed__ungrouped'.replace('ungrouped', '__ungrouped__'))
  })
})
