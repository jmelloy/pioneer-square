import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import DashboardView from '../DashboardView.vue'
import { useAgentsStore } from '../../stores/agents'
import { useTasksStore } from '../../stores/tasks'
import { useUiStore } from '../../stores/ui'
import type { Agent } from '../../types'

const agent = (id: string, state: Agent['state'], logs: Agent['logs'] = []): Agent => ({
  id,
  name: `Bot ${id}`,
  type: 'worker',
  workerId: 'w-1',
  state,
  logs,
  joinedAt: '',
})

describe('DashboardView', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('renders workers, newest tasks first, and merged activity newest first', async () => {
    const agents = useAgentsStore()
    agents.agents = [
      agent('a', 'working', [{ line: 'old line', timestamp: '2026-01-01T00:00:00Z' }]),
      agent('b', 'idle', [{ line: 'new line', timestamp: '2026-01-02T00:00:00Z' }]),
    ]
    useTasksStore().tasks = [
      { id: 't-old', name: 'Old task', state: 'done', created_at: '2026-01-01T00:00:00Z' },
      { id: 't-new', name: 'New task', state: 'working', created_at: '2026-01-02T00:00:00Z' },
    ]
    const ui = useUiStore()
    const w = mount(DashboardView, { global: { stubs: { FactoryFloor: true } } })

    expect(w.find('.stat-working .stat-value').text()).toBe('1')
    expect(w.find('.stat-idle .stat-value').text()).toBe('1')

    const taskNames = w.findAll('.base-card .name').map((n) => n.text())
    expect(taskNames).toContain('Bot a')
    expect(taskNames.indexOf('New task')).toBeLessThan(taskNames.indexOf('Old task'))

    const lines = w.findAll('.event .line').map((n) => n.text())
    expect(lines).toEqual(['new line', 'old line'])

    await w.findAll('.event .who')[0].trigger('click')
    expect(ui.activeTab).toBe('agent-b')

    const taskRow = w.findAll('button.row').find((b) => b.text().includes('New task'))!
    await taskRow.trigger('click')
    expect(ui.activeTab).toBe('task-t-new')
  })
})
