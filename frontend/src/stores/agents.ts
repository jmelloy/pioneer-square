import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { useGuildStore } from './guild'
import { api } from '../utils/api'
import { formatWorkerId } from '../utils/format'
import { taskTabId } from './tasks'
import type {
  Agent,
  AgentActivity,
  AgentState,
  LogDetail,
  LogEntry,
  LogLevel,
  Worker,
  WSInbound,
} from '../types'

const STATE_RANK: Record<string, number> = {
  working: 0,
  thinking: 1,
  busy: 2,
  error: 3,
  idle: 4,
  offline: 5,
}

interface RegisterAgentData {
  agentId: string
  agentName?: string
  agentType?: string
  workerId?: string | null
  workerName?: string
  state?: AgentState
  taskId?: string | null
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
  name?: string | null
  phase?: string | null
  issueNumber?: number | null
  issueRepo?: string | null
}

export function issueTabId(owner: string, repo: string, number: number): string {
  return `issue-${owner}/${repo}/${number}`
}

export const useAgentsStore = defineStore('agents', () => {
  const agents = ref<Agent[]>([])
  const workerLogs = ref<Record<string, LogEntry[]>>({})
  const selectedWorkerId = ref<string | null>(null)
  const openedWorkerIds = ref<string[]>([])
  const selectedAgentId = ref<string | null>(null)
  const openedAgentIds = ref<string[]>([])
  const openedIssueKeys = ref<string[]>([])
  const activeTab = ref<string>('factory')

  // Unique workers derived from agent slots
  const workers = computed<Worker[]>(() => {
    const map = new Map<string, Worker>()
    for (const agent of agents.value) {
      if (!agent.workerId) continue
      if (!map.has(agent.workerId)) {
        map.set(agent.workerId, {
          id: agent.workerId,
          // Prefer the backend-supplied workerName; older backends that omit it
          // fall back to the workerId itself as a stable label.
          name: agent.workerName || agent.workerId,
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
      if (agentData.workerName) existing.workerName = agentData.workerName
      if (agentData.taskId !== undefined) existing.taskId = agentData.taskId
    } else {
      agents.value.push({
        id: agentData.agentId,
        name: agentData.agentName || 'Unknown',
        type: agentData.agentType || 'worker',
        workerId: agentData.workerId || null,
        workerName: agentData.workerName,
        state: agentData.state || 'idle',
        taskId: agentData.taskId ?? null,
        logs: [],
        joinedAt: agentData.joinedAt || new Date().toISOString(),
      })
    }
  }

  function updateAgentState(
    agentId: string,
    state: AgentState,
    activity?: AgentActivity | null,
    taskId?: string | null,
  ): boolean {
    const agent = agents.value.find((a) => a.id === agentId)
    if (!agent) return false
    agent.state = state
    if (activity !== undefined) agent.activity = activity
    if (taskId !== undefined) agent.taskId = taskId
    // Idle/offline agents are by definition not working a task; clear the
    // link so a stale taskId from a prior run can't latch the agent to a row.
    if (state === 'idle' || state === 'offline') agent.taskId = null
    return true
  }

  // Resets the agent responsible for a completed task back to idle.
  // Tries agentId first; falls through to taskId lookup on a miss (e.g. race
  // during deregistration where agentId is present but no longer registered).
  function resetAgentForTask(data: { agentId?: string | null; taskId?: string }) {
    if (data.agentId && updateAgentState(data.agentId, 'idle', null, null)) return
    const match = agents.value.find((a) => a.taskId === data.taskId)
    if (match) updateAgentState(match.id, 'idle', null, null)
  }

  function addLog(
    agentId: string,
    line: string,
    timestamp?: string,
    detail?: LogDetail | null,
    level?: LogLevel | null,
  ) {
    const agent = agents.value.find((a) => a.id === agentId)
    if (agent && line) {
      const ts = timestamp || new Date().toISOString()
      agent.logs.push({ line, timestamp: ts, detail: detail || null, level: level || null })
      if (agent.logs.length > 500) agent.logs.shift()
    }
  }

  function addWorkerLog(
    workerId: string,
    line: string,
    timestamp?: string,
    detail?: LogDetail | null,
    level?: LogLevel | null,
  ) {
    if (!line) return
    if (!workerLogs.value[workerId]) workerLogs.value[workerId] = []
    const ts = timestamp || new Date().toISOString()
    workerLogs.value[workerId].push({
      line,
      timestamp: ts,
      detail: detail || null,
      level: level || null,
    })
    if (workerLogs.value[workerId].length > 500) workerLogs.value[workerId].shift()
  }

  function selectWorker(workerId: string | null) {
    selectedWorkerId.value = workerId
    if (workerId) {
      if (!openedWorkerIds.value.includes(workerId)) openedWorkerIds.value.push(workerId)
      activeTab.value = 'worker-' + workerId
    }
  }

  function closeWorker(workerId: string) {
    const idx = openedWorkerIds.value.indexOf(workerId)
    if (idx !== -1) openedWorkerIds.value.splice(idx, 1)
    if (selectedWorkerId.value === workerId) selectedWorkerId.value = null
    if (activeTab.value === 'worker-' + workerId) activeTab.value = 'factory'
  }

  function selectAgent(agentId: string | null) {
    selectedAgentId.value = agentId
    if (agentId) {
      if (!openedAgentIds.value.includes(agentId)) openedAgentIds.value.push(agentId)
      activeTab.value = 'agent-' + agentId
    }
  }

  function closeAgent(agentId: string) {
    const idx = openedAgentIds.value.indexOf(agentId)
    if (idx !== -1) openedAgentIds.value.splice(idx, 1)
    if (selectedAgentId.value === agentId) selectedAgentId.value = null
    if (activeTab.value === 'agent-' + agentId) activeTab.value = 'factory'
  }

  function openTaskTab(taskId: string) {
    activeTab.value = taskTabId(taskId)
  }

  function openIssueTab(owner: string, repo: string, number: number) {
    const key = issueTabId(owner, repo, number)
    if (!openedIssueKeys.value.includes(key)) openedIssueKeys.value.push(key)
    activeTab.value = key
  }

  function closeIssueTab(key: string) {
    const idx = openedIssueKeys.value.indexOf(key)
    if (idx !== -1) openedIssueKeys.value.splice(idx, 1)
    if (activeTab.value === key) activeTab.value = 'factory'
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
    { description, name, phase, issueNumber, issueRepo }: AssignTaskOpts,
  ) {
    const guildId = _currentGuildId()
    return api(`/guilds/${guildId}/workers/${workerId}/tasks`, {
      method: 'POST',
      json: {
        description,
        name: name || null,
        phase: phase ?? null,
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
    const pick =
      workerAgents.find((a) => a.state === 'idle') ||
      workerAgents.find((a) => !['working', 'error'].includes(a.state)) ||
      workerAgents[0]
    return pick ? { id: pick.workerId, name: pick.name, state: pick.state } : null
  }

  // Returns the stored display name (e.g. "myhost/VD-3566") for a worker,
  // falling back to the droid-style ID for workers not yet seen via WebSocket.
  function workerDisplayName(workerId: string): string {
    const worker = workers.value.find((w) => w.id === workerId)
    return worker?.name ?? formatWorkerId(workerId)
  }

  function clearAgents() {
    agents.value = []
    workerLogs.value = {}
    selectedWorkerId.value = null
    openedWorkerIds.value = []
    selectedAgentId.value = null
    openedAgentIds.value = []
    activeTab.value = 'factory'
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
      const agent = agents.value.find((a) => a.id === agentId)
      if (agent) {
        const historical = raw.map(_toLogEntry)
        agent.logs = historical.length > 2000 ? historical.slice(-2000) : historical
      }
    } catch (e) {
      console.error('Failed to fetch agent logs', e)
    }
  }

  function handleWebSocketMessage(data: WSInbound) {
    if (data.type === 'agent-joined') {
      registerAgent(data)
    } else if (data.type === 'agent-state') {
      updateAgentState(
        data.agentId,
        data.state,
        data.activity ?? undefined,
        data.taskId ?? undefined,
      )
    } else if (data.type === 'task-complete') {
      // The worker returns to its idle pool immediately after sending task-complete.
      // Reset the agent proactively so the sidebar doesn't lag behind waiting for
      // the separate agent-state:idle frame (which may be delayed for short tasks).
      resetAgentForTask(data)
    } else if (data.type === 'task-followup-done') {
      // Same as task-complete: worker returns to idle after sending this.
      resetAgentForTask(data)
    } else if (data.type === 'terminal-output') {
      // Route to per-agent log buffer (includes task logs for agent-tab view)
      if (data.agentId) addLog(data.agentId, data.line, data.timestamp, data.detail, data.level)
      // Route to per-worker log buffer; worker-wide lines may arrive with only
      // workerId, while agent/task lines include both workerId and agentId.
      const wid =
        data.workerId ||
        (data.agentId ? agents.value.find((a) => a.id === data.agentId)?.workerId : null)
      if (wid) addWorkerLog(wid, data.line, data.timestamp, data.detail, data.level)
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
    openedIssueKeys,
    activeTab,
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
    openTaskTab,
    openIssueTab,
    closeIssueTab,
    sendMessage,
    runAgent,
    stopAgent,
    assignTask,
    messageWorker,
    firstIdleWorker,
    workerDisplayName,
    clearAgents,
    handleWebSocketMessage,
  }
})
