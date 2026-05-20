import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '../utils/api'
import type { LogEntry, Task, TaskState, WSInbound } from '../types'

export function taskTabId(id: string): string {
  return 'task-' + id
}

// Cap on setTimeout delay to avoid the 32-bit overflow that fires the timer
// immediately on long horizons (e.g. a 3-day finalize window).
const MAX_TIMEOUT_MS = 2_147_483_647

const TERMINAL_STATES = new Set<string>(['done', 'failed', 'cancelled'])

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

  // Per-task timers that drop the row when its soft-delete window elapses.
  const _expiryTimers = new Map<string, ReturnType<typeof setTimeout>>()

  function _removeTask(taskId: string) {
    const idx = tasks.value.findIndex((t) => t.id === taskId)
    if (idx >= 0) tasks.value.splice(idx, 1)
    delete taskLogs.value[taskId]
    closeTask(taskId)
    const timer = _expiryTimers.get(taskId)
    if (timer) {
      clearTimeout(timer)
      _expiryTimers.delete(taskId)
    }
  }

  function _scheduleExpiry(taskId: string, deletedAt: string | null | undefined) {
    const existing = _expiryTimers.get(taskId)
    if (existing) {
      clearTimeout(existing)
      _expiryTimers.delete(taskId)
    }
    if (!deletedAt) return
    const delay = new Date(deletedAt).getTime() - Date.now()
    if (Number.isNaN(delay)) return
    if (delay <= 0) {
      _removeTask(taskId)
      return
    }
    const timer = setTimeout(() => _removeTask(taskId), Math.min(delay, MAX_TIMEOUT_MS))
    _expiryTimers.set(taskId, timer)
  }

  async function fetchTasks(guildId: string) {
    if (!guildId) return
    try {
      tasks.value = await api<Task[]>(`/guilds/${guildId}/tasks`)
      for (const t of tasks.value) {
        if (t.deleted_at) _scheduleExpiry(t.id, t.deleted_at)
      }
    } catch (e) {
      console.error('Failed to fetch tasks', e)
    }
  }

  async function fetchTaskLogs(guildId: string, taskId: string) {
    try {
      const raw = await api<Array<{ line: string; timestamp: string; detail?: unknown }>>(
        `/guilds/${guildId}/tasks/${taskId}/logs`,
      )
      const logs: LogEntry[] = raw.map((r) => ({
        line: r.line,
        timestamp: r.timestamp,
        detail: (r.detail as LogEntry['detail']) || null,
      }))
      taskLogs.value[taskId] = logs
      return logs
    } catch (e) {
      console.error('Failed to fetch task logs', e)
      return []
    }
  }

  async function sendFollowup(guildId: string, taskId: string, instructions: string) {
    return api(`/guilds/${guildId}/tasks/${taskId}/followup`, {
      method: 'POST',
      json: { instructions },
    })
  }

  async function finalizeTask(guildId: string, taskId: string) {
    return api(`/guilds/${guildId}/tasks/${taskId}/finalize`, { method: 'POST' })
  }

  async function cancelTask(guildId: string, taskId: string) {
    const task = tasks.value.find((t) => t.id === taskId)
    const prevState = task?.state
    const prevFinishedAt = task?.finished_at
    if (task) {
      task.state = 'cancelled'
      task.finished_at = new Date().toISOString()
    }
    try {
      await api(`/guilds/${guildId}/tasks/${taskId}/cancel`, { method: 'POST' })
    } catch (err) {
      if (task) {
        task.state = prevState!
        task.finished_at = prevFinishedAt
      }
      throw err
    }
  }

  async function redirectTask(guildId: string, taskId: string, instructions: string) {
    return api(`/guilds/${guildId}/tasks/${taskId}/redirect`, {
      method: 'POST',
      json: { instructions },
    })
  }

  function _upsertTask(data: Task) {
    const idx = tasks.value.findIndex((t) => t.id === data.id)
    if (idx >= 0) {
      Object.assign(tasks.value[idx], data)
    } else {
      tasks.value.unshift(data)
    }
  }

  function handleWebSocketMessage(data: WSInbound) {
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
        if (data.deletedAt !== undefined) {
          task.deleted_at = data.deletedAt
          _scheduleExpiry(task.id, data.deletedAt)
        }
      }
    } else if (data.type === 'task-complete') {
      const task = tasks.value.find((t) => t.id === data.taskId)
      if (task && !TERMINAL_STATES.has(task.state)) {
        task.state = 'awaiting-review'
        if (data.branch) task.branch = data.branch
      }
    } else if (data.type === 'task-followup-done') {
      const task = tasks.value.find((t) => t.id === data.taskId)
      if (task && !TERMINAL_STATES.has(task.state)) task.state = 'awaiting-review'
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
    for (const timer of _expiryTimers.values()) clearTimeout(timer)
    _expiryTimers.clear()
  }

  // Tasks whose soft-delete window has not yet elapsed. Components that filter
  // for display should prefer this over `tasks` so a row disappears the moment
  // its `deleted_at` passes, even before the per-task timer fires.
  const liveTasks = computed(() => {
    const now = Date.now()
    return tasks.value.filter((t) => {
      if (!t.deleted_at) return true
      const ts = new Date(t.deleted_at).getTime()
      return Number.isNaN(ts) || ts > now
    })
  })

  function stateLabel(state: TaskState | string) {
    return STATE_LABELS[state] || state
  }
  function stateColor(state: TaskState | string) {
    return STATE_COLORS[state] || 'dim'
  }

  return {
    tasks,
    liveTasks,
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
