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

    it('removes the agent entirely when it goes offline', () => {
      // Regression for #731: offline slots must be pruned rather than kept
      // around with a cleared taskId, or a worker that restarts repeatedly
      // accumulates one dead row per restart.
      const store = useAgentsStore()
      store.registerAgent({ agentId: 'a-1', workerId: 'w-x', state: 'working', taskId: 't-1' })

      store.handleWebSocketMessage({
        type: 'agent-state',
        agentId: 'a-1',
        state: 'offline',
      })

      expect(store.agents).toHaveLength(0)
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

  describe('terminal-output routing', () => {
    it('routes worker-only output to worker logs without attaching it to an agent', () => {
      const store = useAgentsStore()

      store.handleWebSocketMessage({
        type: 'terminal-output',
        workerId: 'w-x',
        line: '[worker] Pulled repo',
        timestamp: '2026-05-20T00:00:00.000Z',
      })

      expect(store.workerLogs['w-x']).toHaveLength(1)
      expect(store.workerLogs['w-x'][0].line).toBe('[worker] Pulled repo')
      expect(store.agents).toHaveLength(0)
    })

    it('threads the typed level through to the stored worker log entry', () => {
      const store = useAgentsStore()

      store.handleWebSocketMessage({
        type: 'terminal-output',
        workerId: 'w-x',
        line: 'Online. Watching for tasks.',
        level: 'worker',
        timestamp: '2026-05-20T00:00:00.000Z',
      })

      expect(store.workerLogs['w-x'][0].level).toBe('worker')
    })

    it('routes task output to both the agent log and the worker log', () => {
      const store = useAgentsStore()
      store.registerAgent({ agentId: 'a-1', workerId: 'w-x', state: 'working', taskId: 't-1' })

      store.handleWebSocketMessage({
        type: 'terminal-output',
        workerId: 'w-x',
        agentId: 'a-1',
        taskId: 't-1',
        line: '[claude] editing',
        timestamp: '2026-05-20T00:00:00.000Z',
      })

      expect(store.agents[0].logs).toHaveLength(1)
      expect(store.agents[0].logs[0].line).toBe('[claude] editing')
      expect(store.workerLogs['w-x']).toHaveLength(1)
      expect(store.workerLogs['w-x'][0].line).toBe('[claude] editing')
    })
  })

  describe('task-complete and task-followup-done reset agent to idle', () => {
    it('resets agent to idle on task-complete when agentId is present', () => {
      const store = useAgentsStore()
      store.registerAgent({ agentId: 'a-1', workerId: 'w-x', state: 'working', taskId: 't-1' })

      store.handleWebSocketMessage({
        type: 'task-complete',
        taskId: 't-1',
        agentId: 'a-1',
      })

      expect(store.agents[0].state).toBe('idle')
      expect(store.agents[0].taskId).toBeNull()
    })

    it('resets agent to idle on task-complete by taskId fallback when agentId is absent', () => {
      const store = useAgentsStore()
      store.registerAgent({ agentId: 'a-1', workerId: 'w-x', state: 'working', taskId: 't-2' })

      store.handleWebSocketMessage({
        type: 'task-complete',
        taskId: 't-2',
      })

      expect(store.agents[0].state).toBe('idle')
      expect(store.agents[0].taskId).toBeNull()
    })

    it('resets agent to idle on task-followup-done when agentId is present', () => {
      const store = useAgentsStore()
      store.registerAgent({ agentId: 'a-1', workerId: 'w-x', state: 'working', taskId: 't-3' })

      store.handleWebSocketMessage({
        type: 'task-followup-done',
        taskId: 't-3',
        agentId: 'a-1',
      })

      expect(store.agents[0].state).toBe('idle')
      expect(store.agents[0].taskId).toBeNull()
    })

    it('resets agent to idle on task-followup-done by taskId fallback when agentId is absent', () => {
      const store = useAgentsStore()
      store.registerAgent({ agentId: 'a-1', workerId: 'w-x', state: 'working', taskId: 't-4' })

      store.handleWebSocketMessage({
        type: 'task-followup-done',
        taskId: 't-4',
      })

      expect(store.agents[0].state).toBe('idle')
      expect(store.agents[0].taskId).toBeNull()
    })

    it('is a no-op for task-complete when neither agentId nor matching taskId exists', () => {
      const store = useAgentsStore()
      store.registerAgent({ agentId: 'a-1', workerId: 'w-x', state: 'working', taskId: 't-99' })

      store.handleWebSocketMessage({
        type: 'task-complete',
        taskId: 't-unknown',
      })

      expect(store.agents[0].state).toBe('working')
    })

    it('handles a subsequent agent-state:idle gracefully after task-complete already cleared the agent (double-update)', () => {
      const store = useAgentsStore()
      store.registerAgent({ agentId: 'a-1', workerId: 'w-x', state: 'working', taskId: 't-5' })

      // First frame: task-complete sets agent to idle and clears taskId
      store.handleWebSocketMessage({ type: 'task-complete', taskId: 't-5', agentId: 'a-1' })
      expect(store.agents[0].state).toBe('idle')
      expect(store.agents[0].taskId).toBeNull()

      // Second frame: trailing agent-state:idle arrives; taskId is already null
      // so the taskId-based lookup would be a no-op — but the agentId lookup
      // must still find the agent and leave it idle without throwing.
      store.handleWebSocketMessage({ type: 'agent-state', agentId: 'a-1', state: 'idle' })
      expect(store.agents[0].state).toBe('idle')
      expect(store.agents[0].taskId).toBeNull()
    })

    it('falls through to taskId lookup when agentId is present but not registered (deregistration race)', () => {
      const store = useAgentsStore()
      // Agent is registered with a taskId but its agentId will not match the
      // one in the task-complete message (simulates a race where the ws frame
      // carries a stale agentId while the slot reconnected under a new id).
      store.registerAgent({ agentId: 'a-new', workerId: 'w-x', state: 'working', taskId: 't-6' })

      store.handleWebSocketMessage({
        type: 'task-complete',
        taskId: 't-6',
        agentId: 'a-old', // not registered
      })

      // agentId lookup misses; taskId fallback must find a-new and reset it
      expect(store.agents[0].state).toBe('idle')
      expect(store.agents[0].taskId).toBeNull()
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
