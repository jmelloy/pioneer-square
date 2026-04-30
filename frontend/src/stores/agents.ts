import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { useGuildStore } from './guild'
import { useAuthStore } from './auth'
import type { Agent, AgentState, LogEntry, Worker, WSMessage } from '../types'

const API_BASE = (import.meta.env.VITE_API_BASE as string) ?? ''

const STATE_RANK: Record<string, number> = {
  working: 0,
  thinking: 1,
  busy: 2,
  error: 3,
  'awaiting-review': 4,
  idle: 5,
  offline: 6,
}

interface RegisterAgentData {
  agentId: string
  agentName?: string
  agentType?: string
  workerId?: string | null
  state?: AgentState
  joinedAt?: string
}

interface RunAgentOpts {
  tool: string
  prompt: string
  model?: string
  provider?: string
}

interface AssignTaskOpts {
  description: string
  issueNumber?: number | null
  issueRepo?: string | null
}

export const useAgentsStore = defineStore('agents', () => {
  const agents = ref<Agent[]>([])
  const workerLogs = ref<Record<string, LogEntry[]>>({})
  const selectedWorkerId = ref<string | null>(null)
  const openedWorkerIds = ref<string[]>([])
  const selectedAgentId = ref<string | null>(null)
  const openedAgentIds = ref<string[]>([])

  // Unique workers derived from agent slots
  const workers = computed<Worker[]>(() => {
    const map = new Map<string, Worker>()
    for (const agent of agents.value) {
      if (!agent.workerId) continue
      if (!map.has(agent.workerId)) {
        map.set(agent.workerId, {
          id: agent.workerId,
          name: agent.name.replace(/\/\d+$/, ''),
          state: agent.state,
        })
      } else {
        const w = map.get(agent.workerId)!
        if ((STATE_RANK[agent.state] ?? 7) < (STATE_RANK[w.state] ?? 7)) {
          w.state = agent.state
        }
      }
    }
    return Array.from(map.values())
  })

  function registerAgent(agentData: RegisterAgentData) {
    if (agentData.agentType === 'foreman') return
    const existing = agents.value.find(a => a.id === agentData.agentId)
    if (existing) {
      existing.state = agentData.state || 'idle'
      existing.name = agentData.agentName || existing.name
      if (agentData.workerId) existing.workerId = agentData.workerId
    } else {
      agents.value.push({
        id: agentData.agentId,
        name: agentData.agentName || 'Unknown',
        type: agentData.agentType || 'worker',
        workerId: agentData.workerId || null,
        state: agentData.state || 'idle',
        logs: [],
        joinedAt: agentData.joinedAt || new Date().toISOString()
      })
    }
  }

  function updateAgentState(agentId: string, state: AgentState) {
    const agent = agents.value.find(a => a.id === agentId)
    if (agent) agent.state = state
  }

  function addLog(agentId: string, line: string, timestamp?: string, detail?: any) {
    const agent = agents.value.find(a => a.id === agentId)
    if (agent && line) {
      const ts = timestamp || new Date().toISOString()
      agent.logs.push({ line, timestamp: ts, detail: detail || null })
      if (agent.logs.length > 500) agent.logs.shift()
    }
  }

  function addWorkerLog(workerId: string, line: string, timestamp?: string, detail?: any) {
    if (!line) return
    if (!workerLogs.value[workerId]) workerLogs.value[workerId] = []
    const ts = timestamp || new Date().toISOString()
    workerLogs.value[workerId].push({ line, timestamp: ts, detail: detail || null })
    if (workerLogs.value[workerId].length > 500) workerLogs.value[workerId].shift()
  }

  function selectWorker(workerId: string | null) {
    selectedWorkerId.value = workerId
    if (workerId && !openedWorkerIds.value.includes(workerId)) {
      openedWorkerIds.value.push(workerId)
    }
  }

  function closeWorker(workerId: string) {
    const idx = openedWorkerIds.value.indexOf(workerId)
    if (idx !== -1) openedWorkerIds.value.splice(idx, 1)
    if (selectedWorkerId.value === workerId) selectedWorkerId.value = null
  }

  function selectAgent(agentId: string | null) {
    selectedAgentId.value = agentId
    if (agentId && !openedAgentIds.value.includes(agentId)) {
      openedAgentIds.value.push(agentId)
    }
  }

  function closeAgent(agentId: string) {
    const idx = openedAgentIds.value.indexOf(agentId)
    if (idx !== -1) openedAgentIds.value.splice(idx, 1)
    if (selectedAgentId.value === agentId) selectedAgentId.value = null
  }

  function sendMessage(agentId: string, content: string) {
    const guildStore = useGuildStore()
    guildStore.sendMessage({
      type: 'chat',
      from: 'user',
      to: agentId,
      content
    })
  }

  function _authHeaders(): Record<string, string> {
    return useAuthStore().authHeaders()
  }

  async function runAgent(agentId: string, { tool, prompt, model, provider }: RunAgentOpts) {
    const guildStore = useGuildStore()
    const guildId = guildStore.currentGuild?.id
    if (!guildId) throw new Error('No active guild')

    const res = await fetch(`${API_BASE}/guilds/${guildId}/agents/${agentId}/run`, {
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

  async function stopAgent(agentId: string) {
    const guildStore = useGuildStore()
    const guildId = guildStore.currentGuild?.id
    if (!guildId) throw new Error('No active guild')

    const res = await fetch(`${API_BASE}/guilds/${guildId}/agents/${agentId}/run`, {
      method: 'DELETE',
      headers: _authHeaders(),
    })
    if (!res.ok && res.status !== 404) {
      const err = await res.json().catch(() => ({}))
      throw new Error(err.detail || `HTTP ${res.status}`)
    }
    return res.json()
  }

  async function assignTask(workerId: string, { description, issueNumber, issueRepo }: AssignTaskOpts) {
    const guildStore = useGuildStore()
    const guildId = guildStore.currentGuild?.id
    if (!guildId) throw new Error('No active guild')

    const res = await fetch(`${API_BASE}/guilds/${guildId}/workers/${workerId}/tasks`, {
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

  async function messageWorker(workerId: string, message: string) {
    const guildStore = useGuildStore()
    const guildId = guildStore.currentGuild?.id
    if (!guildId) throw new Error('No active guild')

    const res = await fetch(`${API_BASE}/guilds/${guildId}/workers/${workerId}/message`, {
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

  function firstIdleWorker() {
    const workerAgents = agents.value.filter(
      a => a.workerId && a.state !== 'offline'
    )
    const idleAgent =
      workerAgents.find(a => a.state === 'idle') ||
      workerAgents.find(a => !['working', 'awaiting-review', 'error'].includes(a.state)) ||
      workerAgents[0]
    if (idleAgent) return { id: idleAgent.workerId, name: idleAgent.name, state: idleAgent.state }

    const legacy = agents.value.filter(a => a.id.startsWith('w-') && a.state !== 'offline')
    return legacy.find(a => a.state === 'idle') || legacy[0] || null
  }

  function clearAgents() {
    agents.value = []
    workerLogs.value = {}
    selectedWorkerId.value = null
    openedWorkerIds.value = []
    selectedAgentId.value = null
    openedAgentIds.value = []
  }

  async function fetchWorkerLogs(guildId: string, workerId: string) {
    try {
      const res = await fetch(`${API_BASE}/guilds/${guildId}/logs?worker_id=${workerId}`)
      if (res.ok) {
        const raw = await res.json()
        workerLogs.value[workerId] = raw.map((r: any) => ({
          line: r.line,
          timestamp: r.timestamp,
          detail: r.detail || null,
        }))
      }
    } catch (e) {
      console.error('Failed to fetch worker logs', e)
    }
  }

  async function fetchAgentLogs(guildId: string, agentId: string) {
    try {
      const res = await fetch(`${API_BASE}/guilds/${guildId}/logs?agent_id=${agentId}`)
      if (res.ok) {
        const raw = await res.json()
        const historical: LogEntry[] = raw.map((r: any) => ({
          line: r.line,
          timestamp: r.timestamp,
          detail: r.detail || null,
        }))
        const agent = agents.value.find(a => a.id === agentId)
        if (agent) {
          const existing = agent.logs
          agent.logs = [...historical, ...existing]
          if (agent.logs.length > 2000) agent.logs = agent.logs.slice(-2000)
        }
      }
    } catch (e) {
      console.error('Failed to fetch agent logs', e)
    }
  }

  function handleWebSocketMessage(data: WSMessage) {
    if (data.type === 'agent-joined') {
      registerAgent(data as RegisterAgentData)
    } else if (data.type === 'agent-state') {
      updateAgentState(data.agentId, data.state)
    } else if (data.type === 'terminal-output') {
      // Route to per-agent log buffer (includes task logs for agent-tab view)
      if (data.agentId) addLog(data.agentId, data.line, data.timestamp, data.detail)
      // Route to per-worker log buffer; fall back to agent's workerId if backend didn't send it
      const wid = data.workerId || (data.agentId
        ? agents.value.find(a => a.id === data.agentId)?.workerId
        : null)
      if (wid) addWorkerLog(wid, data.line, data.timestamp, data.detail)
    }
  }

  return {
    agents,
    workers,
    workerLogs,
    selectedWorkerId,
    openedWorkerIds,
    selectedAgentId,
    openedAgentIds,
    registerAgent,
    updateAgentState,
    addLog,
    addWorkerLog,
    fetchWorkerLogs,
    fetchAgentLogs,
    selectWorker,
    closeWorker,
    selectAgent,
    closeAgent,
    sendMessage,
    runAgent,
    stopAgent,
    assignTask,
    messageWorker,
    firstIdleWorker,
    clearAgents,
    handleWebSocketMessage
  }
})
