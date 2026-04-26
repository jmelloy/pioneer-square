import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useSessionStore } from './session'

const API_BASE = 'http://localhost:8000'

export const useAgentsStore = defineStore('agents', () => {
  const agents = ref([])

  function registerAgent(agentData) {
    const existing = agents.value.find(a => a.id === agentData.agentId)
    if (existing) {
      existing.state = agentData.state || 'idle'
      existing.name = agentData.agentName || existing.name
    } else {
      agents.value.push({
        id: agentData.agentId,
        name: agentData.agentName || 'Unknown',
        type: agentData.agentType || 'worker',
        state: agentData.state || 'idle',
        logs: [],
        joinedAt: agentData.joinedAt || new Date().toISOString(),
        lastDone: null,
      })
    }
  }

  function updateAgentState(agentId, state) {
    const agent = agents.value.find(a => a.id === agentId)
    if (agent) agent.state = state
  }

  function addLog(agentId, line, timestamp) {
    const agent = agents.value.find(a => a.id === agentId)
    if (agent) {
      agent.logs.push({ line, timestamp: timestamp || new Date().toISOString() })
      if (agent.logs.length > 500) agent.logs.shift()
    }
  }

  function markDone(agentId, { tool, exitCode, summary }) {
    const agent = agents.value.find(a => a.id === agentId)
    if (agent) {
      agent.lastDone = { tool, exitCode, summary, at: new Date().toISOString() }
    }
  }

  function sendMessage(agentId, content) {
    const sessionStore = useSessionStore()
    sessionStore.sendMessage({
      type: 'chat',
      from: 'user',
      to: agentId,
      content
    })
  }

  async function runAgent(agentId, { tool, prompt, model, provider }) {
    const sessionStore = useSessionStore()
    const sessionId = sessionStore.currentSession?.id
    if (!sessionId) throw new Error('No active session')

    const res = await fetch(`${API_BASE}/sessions/${sessionId}/agents/${agentId}/run`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tool, prompt, model: model || undefined, provider: provider || undefined })
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({}))
      throw new Error(err.detail || `HTTP ${res.status}`)
    }
    return res.json()
  }

  async function stopAgent(agentId) {
    const sessionStore = useSessionStore()
    const sessionId = sessionStore.currentSession?.id
    if (!sessionId) throw new Error('No active session')

    const res = await fetch(`${API_BASE}/sessions/${sessionId}/agents/${agentId}/run`, {
      method: 'DELETE'
    })
    if (!res.ok && res.status !== 404) {
      const err = await res.json().catch(() => ({}))
      throw new Error(err.detail || `HTTP ${res.status}`)
    }
    return res.json()
  }

  function handleWebSocketMessage(data) {
    if (data.type === 'agent-joined') {
      registerAgent(data)
    } else if (data.type === 'agent-state') {
      updateAgentState(data.agentId, data.state)
    } else if (data.type === 'terminal-output') {
      addLog(data.agentId, data.line, data.timestamp)
    } else if (data.type === 'agent-done') {
      markDone(data.agentId, {
        tool: data.tool,
        exitCode: data.exitCode,
        summary: data.summary,
      })
      // state is already updated to idle/error by agent-state event
    }
  }

  return {
    agents,
    registerAgent,
    updateAgentState,
    addLog,
    markDone,
    sendMessage,
    runAgent,
    stopAgent,
    handleWebSocketMessage
  }
})
