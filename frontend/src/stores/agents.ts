import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { useGuildStore } from './guild'
import { api } from '../utils/api'
import type { Agent, AgentActivity, AgentState, LogDetail, LogEntry, Worker, WSInbound } from '../types'

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

// Mirrors backend/utils.py worker_display_name() (without hostname).
// Deriving the worker label from workerId rather than the first agent's name
// prevents slot 0's droid name from appearing identical to the worker name (#283).
function _workerDroidName(workerId: string): string {
  const raw = workerId.slice(2).toUpperCase()
  const charSum = raw.split('').reduce((acc, c) => acc + c.charCodeAt(0), 0)
  const split = 2 + (charSum % 3)
  return `${raw.slice(0, split)}-${raw.slice(split)}`
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
          name: _workerDroidName(agent.workerId),
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
    const existing = agents.value.find((a) => a.id === agentData.agentId)
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
        joinedAt: agentData.joinedAt || new Date().toISOString(),
      })
    }
  }

  function updateAgentState(agentId: string, state: AgentState, activity?: AgentActivity | null) {
    const agent = agents.value.find((a) => a.id === agentId)
    if (!agent) return
    agent.state = state
    if (activity !== undefined) agent.activity = activity
  }

  function addLog(agentId: string, line: string, timestamp?: string, detail?: LogDetail | null) {
    const agent = agents.value.find((a) => a.id === agentId)
    if (agent && line) {
      const ts = timestamp || new Date().toISOString()
      agent.logs.push({ line, timestamp: ts, detail: detail || null })
      if (agent.logs.length > 500) agent.logs.shift()
    }
  }

  function addWorkerLog(workerId: string, line: string, timestamp?: string, detail?: LogDetail | null) {
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
      content,
    })
  }

  function _currentGuildId(): string {
    const guildId = useGuildStore().currentGuild?.id
    if (!guildId) throw new Error('No active guild')
    return guildId
  }

  async function runAgent(agentId: string, { tool, prompt, model, provider }: RunAgentOpts) {
    const guildId = _currentGuildId()
    return api(`/guilds/${guildId}/agents/${agentId}/run`, {
      method: 'POST',
      json: { tool, prompt, model: model || undefined, provider: provider || undefined },
    })
  }

  async function stopAgent(agentId: string) {
    const guildId = _currentGuildId()
    return api(`/guilds/${guildId}/agents/${agentId}/run`, { method: 'DELETE', okStatuses: [404] })
  }

  async function assignTask(
    workerId: string,
    { description, issueNumber, issueRepo }: AssignTaskOpts,
  ) {
    const guildId = _currentGuildId()
    return api(`/guilds/${guildId}/workers/${workerId}/tasks`, {
      method: 'POST',
      json: {
        description,
        issue_number: issueNumber || null,
        issue_repo: issueRepo || null,
      },
    })
  }

  async function messageWorker(workerId: string, message: string) {
    const guildId = _currentGuildId()
    return api(`/guilds/${guildId}/workers/${workerId}/message`, {
      method: 'POST',
      json: { message },
    })
  }

  function firstIdleWorker() {
    const workerAgents = agents.value.filter((a) => a.workerId && a.state !== 'offline')
    const idleAgent =
      workerAgents.find((a) => a.state === 'idle') ||
      workerAgents.find((a) => !['working', 'awaiting-review', 'error'].includes(a.state)) ||
      workerAgents[0]
    if (idleAgent) return { id: idleAgent.workerId, name: idleAgent.name, state: idleAgent.state }

    const legacy = agents.value.filter((a) => a.id.startsWith('w-') && a.state !== 'offline')
    return legacy.find((a) => a.state === 'idle') || legacy[0] || null
  }

  function clearAgents() {
    agents.value = []
    workerLogs.value = {}
    selectedWorkerId.value = null
    openedWorkerIds.value = []
    selectedAgentId.value = null
    openedAgentIds.value = []
  }

  type RawLog = { line: string; timestamp: string; detail?: unknown }
  const _toLogEntry = (r: RawLog): LogEntry => ({
    line: r.line,
    timestamp: r.timestamp,
    detail: (r.detail as LogEntry['detail']) || null,
  })

  async function fetchWorkerLogs(guildId: string, workerId: string) {
    try {
      const raw = await api<RawLog[]>(`/guilds/${guildId}/logs?worker_id=${workerId}`)
      workerLogs.value[workerId] = raw.map(_toLogEntry)
    } catch (e) {
      console.error('Failed to fetch worker logs', e)
    }
  }

  async function fetchAgentLogs(guildId: string, agentId: string) {
    try {
      const raw = await api<RawLog[]>(`/guilds/${guildId}/logs?agent_id=${agentId}`)
      const historical = raw.map(_toLogEntry)
      const agent = agents.value.find((a) => a.id === agentId)
      if (agent) {
        agent.logs = [...historical, ...agent.logs]
        if (agent.logs.length > 2000) agent.logs = agent.logs.slice(-2000)
      }
    } catch (e) {
      console.error('Failed to fetch agent logs', e)
    }
  }

  function handleWebSocketMessage(data: WSInbound) {
    if (data.type === 'agent-joined') {
      registerAgent(data)
    } else if (data.type === 'agent-state') {
      updateAgentState(data.agentId, data.state, data.activity ?? undefined)
    } else if (data.type === 'terminal-output') {
      // Route to per-agent log buffer (includes task logs for agent-tab view)
      if (data.agentId) addLog(data.agentId, data.line, data.timestamp, data.detail)
      // Route to per-worker log buffer; fall back to agent's workerId if backend didn't send it
      const wid =
        data.workerId ||
        (data.agentId ? agents.value.find((a) => a.id === data.agentId)?.workerId : null)
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
    handleWebSocketMessage,
  }
})
