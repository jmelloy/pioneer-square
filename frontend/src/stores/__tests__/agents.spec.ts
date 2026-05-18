import { beforeEach, describe, expect, it } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useAgentsStore } from '../agents'

describe('useAgentsStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  describe('registerAgent', () => {
    it('captures taskId from the agent-joined payload', () => {
      const store = useAgentsStore()
      store.registerAgent({
        agentId: 'a-1',
        agentName: 'slot-1',
        agentType: 'worker',
        workerId: 'w-x',
        state: 'working',
        taskId: 't-42',
      })
      expect(store.agents).toHaveLength(1)
      expect(store.agents[0].taskId).toBe('t-42')
      expect(store.agents[0].state).toBe('working')
    })

    it('defaults taskId to null when omitted', () => {
      const store = useAgentsStore()
      store.registerAgent({ agentId: 'a-1', workerId: 'w-x' })
      expect(store.agents[0].taskId).toBeNull()
    })

    it('updates taskId on a re-register (worker reconnect)', () => {
      const store = useAgentsStore()
      store.registerAgent({ agentId: 'a-1', workerId: 'w-x', state: 'idle', taskId: null })
      store.registerAgent({ agentId: 'a-1', workerId: 'w-x', state: 'working', taskId: 't-9' })
      expect(store.agents).toHaveLength(1)
      expect(store.agents[0].taskId).toBe('t-9')
    })
  })

  describe('updateAgentState', () => {
    it('writes taskId from an agent-state WS message', () => {
      const store = useAgentsStore()
      store.registerAgent({ agentId: 'a-1', workerId: 'w-x', state: 'idle', taskId: null })

      store.handleWebSocketMessage({
        type: 'agent-state',
        agentId: 'a-1',
        workerId: 'w-x',
        taskId: 't-99',
        state: 'working',
        activity: 'editing',
      })

      const agent = store.agents.find((a) => a.id === 'a-1')!
      expect(agent.taskId).toBe('t-99')
      expect(agent.state).toBe('working')
      expect(agent.activity).toBe('editing')
    })

    it('clears taskId when the agent goes idle', () => {
      // Regression: a slot that finished a task must drop its taskId so the
      // frontend doesn't keep matching it to the just-completed bench.
      const store = useAgentsStore()
      store.registerAgent({ agentId: 'a-1', workerId: 'w-x', state: 'working', taskId: 't-1' })

      store.handleWebSocketMessage({
        type: 'agent-state',
        agentId: 'a-1',
        state: 'idle',
      })

      expect(store.agents[0].taskId).toBeNull()
      expect(store.agents[0].state).toBe('idle')
    })

    it('clears taskId when the agent goes offline', () => {
      const store = useAgentsStore()
      store.registerAgent({ agentId: 'a-1', workerId: 'w-x', state: 'working', taskId: 't-1' })

      store.handleWebSocketMessage({
        type: 'agent-state',
        agentId: 'a-1',
        state: 'offline',
      })

      expect(store.agents[0].taskId).toBeNull()
    })

    it('preserves taskId when going to error (the failure is tied to that task)', () => {
      const store = useAgentsStore()
      store.registerAgent({ agentId: 'a-1', workerId: 'w-x', state: 'working', taskId: 't-bad' })

      store.handleWebSocketMessage({
        type: 'agent-state',
        agentId: 'a-1',
        state: 'error',
      })

      expect(store.agents[0].taskId).toBe('t-bad')
      expect(store.agents[0].state).toBe('error')
    })

    it('ignores agent-state for an unknown agentId', () => {
      const store = useAgentsStore()
      store.handleWebSocketMessage({
        type: 'agent-state',
        agentId: 'a-ghost',
        state: 'working',
        taskId: 't-1',
      })
      expect(store.agents).toHaveLength(0)
    })
  })

  describe('concurrent slots on the same worker', () => {
    it('tracks distinct taskIds per slot even when workerId matches', () => {
      // Regression: prior matching by workerId collapsed concurrent slots
      // onto slot[0]. Now each agent carries its own taskId, so a task→agent
      // lookup by `agent.taskId === task.id` resolves the right slot.
      const store = useAgentsStore()
      store.registerAgent({ agentId: 'a-1', workerId: 'w-shared', state: 'idle' })
      store.registerAgent({ agentId: 'a-2', workerId: 'w-shared', state: 'idle' })

      store.handleWebSocketMessage({
        type: 'agent-state',
        agentId: 'a-1',
        workerId: 'w-shared',
        taskId: 't-A',
        state: 'working',
      })
      store.handleWebSocketMessage({
        type: 'agent-state',
        agentId: 'a-2',
        workerId: 'w-shared',
        taskId: 't-B',
        state: 'working',
      })

      const owners = new Map(store.agents.map((a) => [a.taskId, a.id]))
      expect(owners.get('t-A')).toBe('a-1')
      expect(owners.get('t-B')).toBe('a-2')
    })
  })
})
