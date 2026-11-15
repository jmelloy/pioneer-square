import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import WorkerList from '../WorkerList.vue'
import { useAgentsStore } from '../../../stores/agents'

describe('WorkerList', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    const store = useAgentsStore()
    store.agents = [
      { id: 'a1', name: 'Agent 1', workerId: 'w1', workerName: 'Worker 1', state: 'working' },
      { id: 'a2', name: 'Agent 2', workerId: 'w1', workerName: 'Worker 1', state: 'error' },
    ] as never
  })

  it('collapses a worker to status dots and the whole section to header dots', async () => {
    const wrapper = mount(WorkerList)
    expect(wrapper.findAll('.agent-row')).toHaveLength(2)

    await wrapper.find('.worker-row .collapse-btn').trigger('click')
    expect(wrapper.findAll('.agent-row')).toHaveLength(0)
    const workerDots = wrapper.findAll('.worker-row .status-dots .agent-dot')
    expect(workerDots.map((d) => d.classes())).toEqual([
      expect.arrayContaining(['wdot-working']),
      expect.arrayContaining(['wdot-error']),
    ])

    await wrapper.find('.section-header .collapse-btn').trigger('click')
    expect(wrapper.find('.worker-row').exists()).toBe(false)
    expect(wrapper.findAll('.section-dots .agent-dot')).toHaveLength(2)

    await wrapper.find('.section-header .collapse-btn').trigger('click')
    expect(wrapper.findAll('.worker-row')).toHaveLength(1)
  })
})
