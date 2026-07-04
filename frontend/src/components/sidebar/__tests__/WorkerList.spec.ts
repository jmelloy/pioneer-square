import { beforeEach, describe, expect, it } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { mount } from '@vue/test-utils'
import WorkerList from '../WorkerList.vue'
import { useAgentsStore } from '../../../stores/agents'

describe('WorkerList', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('shows a worker exactly once even when two agent slots share its workerId', () => {
    const agentsStore = useAgentsStore()
    agentsStore.registerAgent({ agentId: 'a-1', workerId: 'w-x', state: 'idle' })
    agentsStore.registerAgent({ agentId: 'a-2', workerId: 'w-x', state: 'idle' })

    const wrapper = mount(WorkerList)
    expect(wrapper.findAll('.worker-row')).toHaveLength(1)
  })

  it('drops the stale agent slot after a worker reconnects under a new agentId', () => {
    // Regression for #731: a worker process restart registers a fresh
    // agentId under the same workerId; the old slot is marked offline but
    // never removed from the store, so the sidebar must filter it out
    // instead of rendering both as separate agent rows.
    const agentsStore = useAgentsStore()
    agentsStore.registerAgent({ agentId: 'a-old', workerId: 'w-x', state: 'idle' })
    agentsStore.handleWebSocketMessage({ type: 'agent-state', agentId: 'a-old', state: 'offline' })
    agentsStore.registerAgent({ agentId: 'a-new', workerId: 'w-x', state: 'idle' })

    const wrapper = mount(WorkerList)
    expect(wrapper.findAll('.worker-row')).toHaveLength(1)
    expect(wrapper.findAll('.agent-row')).toHaveLength(1)
  })

  it('hides an offline agent slot while keeping the online one for the same worker', () => {
    const agentsStore = useAgentsStore()
    agentsStore.registerAgent({ agentId: 'a-online', workerId: 'w-x', state: 'idle' })
    agentsStore.registerAgent({ agentId: 'a-offline', workerId: 'w-x', state: 'idle' })
    agentsStore.handleWebSocketMessage({
      type: 'agent-state',
      agentId: 'a-offline',
      state: 'offline',
    })

    const wrapper = mount(WorkerList)
    expect(wrapper.findAll('.agent-row')).toHaveLength(1)
  })
})
