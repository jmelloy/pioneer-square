import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { LogEntry, Task, TaskState, WSMessage } from '../types'

const API_BASE = (import.meta.env.VITE_API_BASE as string) ?? ''

const STATE_LABELS: Record<string, string> = {
  pending: 'pending',
  planning: 'planning',
  working: 'working',
  'awaiting-review': 'review',
  done: 'done',
  failed: 'failed',
  followup: 'follow-up',
  cancelled: 'cancelled',
}

const STATE_COLORS: Record<string, string> = {
  pending: 'dim',
  planning: 'blue',
  working: 'green',
  'awaiting-review': 'amber',
  done: 'teal',
  failed: 'red',
  followup: 'orange',
  cancelled: 'red',
}

export const useTasksStore = defineStore('tasks', () => {
  const tasks = ref<Task[]>([])
  const taskLogs = ref<Record<string, LogEntry[]>>({})
  const selectedTaskId = ref<string | null>(null)
  const openedTaskIds = ref<string[]>([])

  async function fetchTasks(guildId: string) {
    if (!guildId) return
    try {
      const res = await fetch(`${API_BASE}/guilds/${guildId}/tasks`)
      if (res.ok) tasks.value = await res.json()
    } catch (e) {
      console.error('Failed to fetch tasks', e)
    }
  }

  async function fetchTaskLogs(guildId: string, taskId: string) {
    try {
      const res = await fetch(`${API_BASE}/guilds/${guildId}/tasks/${taskId}/logs`)
      if (!res.ok) return []
      const raw = await res.json()
      const logs: LogEntry[] = raw.map((r: any) => ({
        line: r.line,
        timestamp: r.timestamp,
        detail: r.detail || null,
      }))
      taskLogs.value[taskId] = logs
      return logs
    } catch (e) {
      console.error('Failed to fetch task logs', e)
      return []
    }
  }

  async function sendFollowup(guildId: string, taskId: string, instructions: string) {
    const res = await fetch(`${API_BASE}/guilds/${guildId}/tasks/${taskId}/followup`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ instructions }),
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    return res.json()
  }

  async function finalizeTask(guildId: string, taskId: string) {
    const res = await fetch(`${API_BASE}/guilds/${guildId}/tasks/${taskId}/finalize`, {
      method: 'POST',
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    return res.json()
  }

  async function cancelTask(guildId: string, taskId: string) {
    const res = await fetch(`${API_BASE}/guilds/${guildId}/tasks/${taskId}/cancel`, {
      method: 'POST',
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    return res.json()
  }

  async function redirectTask(guildId: string, taskId: string, instructions: string) {
    const res = await fetch(`${API_BASE}/guilds/${guildId}/tasks/${taskId}/redirect`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ instructions }),
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    return res.json()
  }

  function _upsertTask(data: Task) {
    const idx = tasks.value.findIndex((t) => t.id === data.id)
    if (idx >= 0) {
      Object.assign(tasks.value[idx], data)
    } else {
      tasks.value.unshift(data)
    }
  }

  function handleWebSocketMessage(data: WSMessage) {
    if (data.type === 'task-created') {
      _upsertTask({
        id: data.taskId,
        name: data.name,
        description: data.description,
        phase: data.phase,
        state: data.state,
        worker_id: 'foreman',
        created_at: data.createdAt,
      })
    } else if (data.type === 'task-assigned') {
      const existing = tasks.value.find((t) => t.id === data.taskId)
      _upsertTask({
        id: data.taskId,
        name: data.name || (data.description || '').slice(0, 60),
        description: data.description,
        phase: data.phase || 'execute',
        state: 'pending',
        worker_id: data.workerId,
        parent_task_id: data.parentTaskId || null,
        ...(existing ? {} : { created_at: new Date().toISOString() }),
      })
    } else if (data.type === 'task-update') {
      const task = tasks.value.find((t) => t.id === data.taskId)
      if (task) {
        if (data.state) task.state = data.state
        if (data.branch) task.branch = data.branch
        if (data.prUrl) task.pr_url = data.prUrl
        if (data.finishedAt) task.finished_at = data.finishedAt
        if (data.worktreePath) task.worktree_path = data.worktreePath
      }
    } else if (data.type === 'task-complete') {
      const task = tasks.value.find((t) => t.id === data.taskId)
      if (task) {
        task.state = 'awaiting-review'
        if (data.branch) task.branch = data.branch
      }
    } else if (data.type === 'task-followup-done') {
      const task = tasks.value.find((t) => t.id === data.taskId)
      if (task) task.state = 'awaiting-review'
    } else if (data.type === 'terminal-output' && data.taskId) {
      const { taskId, line, timestamp, detail } = data
      if (line) {
        if (!taskLogs.value[taskId]) taskLogs.value[taskId] = []
        taskLogs.value[taskId].push({ line, timestamp, detail: detail || null })
        if (taskLogs.value[taskId].length > 2000) taskLogs.value[taskId].shift()
      }
    }
  }

  function selectTask(taskId: string | null) {
    selectedTaskId.value = taskId
    if (taskId && !openedTaskIds.value.includes(taskId)) {
      openedTaskIds.value.push(taskId)
    }
  }

  function closeTask(taskId: string) {
    const idx = openedTaskIds.value.indexOf(taskId)
    if (idx !== -1) openedTaskIds.value.splice(idx, 1)
    if (selectedTaskId.value === taskId) selectedTaskId.value = null
  }

  function clearTasks() {
    tasks.value = []
    taskLogs.value = {}
    selectedTaskId.value = null
    openedTaskIds.value = []
  }

  function stateLabel(state: TaskState | string) {
    return STATE_LABELS[state] || state
  }
  function stateColor(state: TaskState | string) {
    return STATE_COLORS[state] || 'dim'
  }

  return {
    tasks,
    taskLogs,
    selectedTaskId,
    openedTaskIds,
    fetchTasks,
    fetchTaskLogs,
    sendFollowup,
    finalizeTask,
    cancelTask,
    redirectTask,
    handleWebSocketMessage,
    selectTask,
    closeTask,
    clearTasks,
    stateLabel,
    stateColor,
  }
})
