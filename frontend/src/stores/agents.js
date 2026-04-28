import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useSessionStore } from './session'
import { useAuthStore } from './auth.js'

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
        joinedAt: agentData.joinedAt || new Date().toISOString()
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

  function sendMessage(agentId, content) {
    const sessionStore = useSessionStore()
    sessionStore.sendMessage({
      type: 'chat',
      from: 'user',
      to: agentId,
      content
    })
  }

  function _authHeaders() {
    return useAuthStore().authHeaders()
  }

  async function runAgent(agentId, { tool, prompt, model, provider }) {
    const sessionStore = useSessionStore()
    const sessionId = sessionStore.currentSession?.id
    if (!sessionId) throw new Error('No active session')

    const res = await fetch(`${API_BASE}/sessions/${sessionId}/agents/${agentId}/run`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ..._authHeaders() },
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
      method: 'DELETE',
      headers: _authHeaders(),
    })
    if (!res.ok && res.status !== 404) {
      const err = await res.json().catch(() => ({}))
      throw new Error(err.detail || `HTTP ${res.status}`)
    }
    return res.json()
  }

  async function deployWorker({ repos }) {
    const sessionStore = useSessionStore()
    const sessionId = sessionStore.currentSession?.id
    if (!sessionId) throw new Error('No active session')

    const res = await fetch(`${API_BASE}/sessions/${sessionId}/workers`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ..._authHeaders() },
      body: JSON.stringify({ repos, github_token: null })
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({}))
      throw new Error(err.detail || `HTTP ${res.status}`)
    }
    return res.json()
  }

  async function assignTask(workerId, { description, issueNumber, issueRepo }) {
    const sessionStore = useSessionStore()
    const sessionId = sessionStore.currentSession?.id
    if (!sessionId) throw new Error('No active session')

    const res = await fetch(`${API_BASE}/sessions/${sessionId}/workers/${workerId}/tasks`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ..._authHeaders() },
      body: JSON.stringify({
        description,
        issue_number: issueNumber || null,
        issue_repo: issueRepo || null,
      })
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({}))
      throw new Error(err.detail || `HTTP ${res.status}`)
    }
    return res.json()
  }

  async function messageWorker(workerId, message) {
    const sessionStore = useSessionStore()
    const sessionId = sessionStore.currentSession?.id
    if (!sessionId) throw new Error('No active session')

    const res = await fetch(`${API_BASE}/sessions/${sessionId}/workers/${workerId}/message`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ..._authHeaders() },
      body: JSON.stringify({ message })
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({}))
      throw new Error(err.detail || `HTTP ${res.status}`)
    }
    return res.json()
  }

  // First idle worker agent, or any worker if none are idle
  function firstIdleWorker() {
    const workers = agents.value.filter(a => a.type === 'worker' && a.id.startsWith('w-'))
    return workers.find(a => a.state === 'idle') || workers[0] || null
  }

  function handleWebSocketMessage(data) {
    if (data.type === 'agent-joined') {
      registerAgent(data)
    } else if (data.type === 'agent-state') {
      updateAgentState(data.agentId, data.state)
    } else if (data.type === 'terminal-output') {
      addLog(data.agentId, data.line, data.timestamp)
    }
    // task-complete and task-failed are handled by ChatPane via the session message handler
  }

  return {
    agents,
    registerAgent,
    updateAgentState,
    addLog,
    sendMessage,
    runAgent,
    stopAgent,
    deployWorker,
    assignTask,
    messageWorker,
    firstIdleWorker,
    handleWebSocketMessage
  }
})
