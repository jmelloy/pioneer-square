import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useGuildStore } from './guild'

const API_BASE = 'http://localhost:8000'

export const useAgentsStore = defineStore('agents', () => {
  const agents = ref([])

  function registerAgent(agentData) {
    if (agentData.agentType === 'foreman') return
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
      const ts = timestamp || new Date().toISOString()
      for (const l of line.split('\n')) {
        agent.logs.push({ line: l, timestamp: ts })
        if (agent.logs.length > 500) agent.logs.shift()
      }
    }
  }

  function sendMessage(agentId, content) {
    const guildStore = useGuildStore()
    guildStore.sendMessage({
      type: 'chat',
      from: 'user',
      to: agentId,
      content
    })
  }

  async function runAgent(agentId, { tool, prompt, model, provider }) {
    const guildStore = useGuildStore()
    const guildId = guildStore.currentGuild?.id
    if (!guildId) throw new Error('No active guild')

    const res = await fetch(`${API_BASE}/guilds/${guildId}/agents/${agentId}/run`, {
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
    const guildStore = useGuildStore()
    const guildId = guildStore.currentGuild?.id
    if (!guildId) throw new Error('No active guild')

    const res = await fetch(`${API_BASE}/guilds/${guildId}/agents/${agentId}/run`, {
      method: 'DELETE'
    })
    if (!res.ok && res.status !== 404) {
      const err = await res.json().catch(() => ({}))
      throw new Error(err.detail || `HTTP ${res.status}`)
    }
    return res.json()
  }

  async function deployWorker({ repos, githubToken }) {
    const guildStore = useGuildStore()
    const guildId = guildStore.currentGuild?.id
    if (!guildId) throw new Error('No active guild')

    const res = await fetch(`${API_BASE}/guilds/${guildId}/workers`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repos, github_token: githubToken || null })
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({}))
      throw new Error(err.detail || `HTTP ${res.status}`)
    }
    return res.json()
  }

  async function assignTask(workerId, { description, issueNumber, issueRepo }) {
    const guildStore = useGuildStore()
    const guildId = guildStore.currentGuild?.id
    if (!guildId) throw new Error('No active guild')

    const res = await fetch(`${API_BASE}/guilds/${guildId}/workers/${workerId}/tasks`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
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
    const guildStore = useGuildStore()
    const guildId = guildStore.currentGuild?.id
    if (!guildId) throw new Error('No active guild')

    const res = await fetch(`${API_BASE}/guilds/${guildId}/workers/${workerId}/message`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
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

  function clearAgents() {
    agents.value = []
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
    clearAgents,
    handleWebSocketMessage
  }
})
