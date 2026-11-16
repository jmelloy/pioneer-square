import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { useGuildStore } from './guild'
import { useUiStore } from './ui'
import { api } from '../utils/api'
import { isVisibleLogEntry } from '../utils/logs'
import type { LogEntry, Task, TaskState, WSInbound } from '../types'

const TERMINAL_STATES = new Set<string>(['done', 'failed', 'cancelled', 'error'])

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
  const uiStore = useUiStore()
  const tasks = ref<Task[]>([])
  const taskLogs = ref<Record<string, LogEntry[]>>({})
  let guildGeneration = 0

  function _removeTask(taskId: string) {
    const idx = tasks.value.findIndex((t) => t.id === taskId)
    if (idx >= 0) tasks.value.splice(idx, 1)
    delete taskLogs.value[taskId]
    uiStore.closeTask(taskId)
  }

  async function fetchTasks(guildId: string): Promise<boolean> {
    if (!guildId) return false
    const generation = guildGeneration
    try {
      const result = await api<Task[]>(`/guilds/${guildId}/tasks`)
      if (generation !== guildGeneration) return false
      tasks.value = result
      return true
    } catch (e) {
      console.error('Failed to fetch tasks', e)
      return false
    }
  }

  async function fetchTaskLogs(guildId: string, taskId: string) {
    const generation = guildGeneration
    try {
      const raw = await api<
        Array<{ line: string; timestamp: string; detail?: unknown; level?: unknown }>
      >(`/guilds/${guildId}/tasks/${taskId}/logs`)
      const logs: LogEntry[] = raw
        .map((r) => ({
          line: r.line,
          timestamp: r.timestamp,
          detail: (r.detail as LogEntry['detail']) || null,
          level: (r.level as LogEntry['level']) || null,
        }))
        .filter(isVisibleLogEntry)
      if (generation !== guildGeneration) return []
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
      // Optimistic: backend stamps finished_at = now() on cancel, which hides
      // the row via liveTasks until the task-update removes it.
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

  async function messageTask(guildId: string, taskId: string, message: string) {
    return api(`/guilds/${guildId}/tasks/${taskId}/message`, {
      method: 'POST',
      json: { message },
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
        task_type: data.taskType,
        state: data.state,
        worker_id: null,
        conversation_id: data.conversationId,
        created_at: data.createdAt,
      })
    } else if (data.type === 'task-assigned') {
      const existing = tasks.value.find((t) => t.id === data.taskId)
      _upsertTask({
        id: data.taskId,
        name: data.name || (data.description || '').slice(0, 60),
        description: data.description,
        phase: data.phase || 'execute',
        task_type: data.taskType || 'standard',
        state: 'pending',
        worker_id: data.workerId,
        parent_task_id: data.parentTaskId || null,
        ...(data.conversationId !== undefined ? { conversation_id: data.conversationId } : {}),
        ...(existing ? {} : { created_at: new Date().toISOString() }),
      })
    } else if (data.type === 'task-update') {
      const task = tasks.value.find((t) => t.id === data.taskId)
      if (task) {
        if (data.state) task.state = data.state
        // A rejected/unassigned task returns to pending with workerId=null
        // (see handle_task_rejected in backend/ws_handlers.py) — without this
        // branch the row kept showing the worker that just gave it back.
        if (data.workerId !== undefined) task.worker_id = data.workerId ?? undefined
        if (data.branch) task.branch = data.branch
        if (data.prUrl) task.pr_url = data.prUrl
        if (data.worktreePath) task.worktree_path = data.worktreePath
        // Finished tasks leave the active list, matching GET /tasks.
        if (data.finishedAt) _removeTask(task.id)
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
      const { taskId, line, timestamp, detail, level } = data
      if (line) {
        const entry = {
          line,
          timestamp,
          detail: detail || null,
          level: level || null,
        }
        if (!isVisibleLogEntry(entry)) return
        if (!taskLogs.value[taskId]) taskLogs.value[taskId] = []
        taskLogs.value[taskId].push(entry)
        if (taskLogs.value[taskId].length > 2000) taskLogs.value[taskId].shift()
      }
    }
  }

  // Declare interest in every inbound WS frame — see subscribeWS in guild.ts,
  // which owns parsing/validation/routing and dispatches here.
  useGuildStore().subscribeWS(handleWebSocketMessage)

  function clearTasks() {
    guildGeneration++
    tasks.value = []
    taskLogs.value = {}
    uiStore.resetTaskSelection()
  }

  // Tasks not yet finished (hides an optimistically cancelled row).
  const liveTasks = computed(() => tasks.value.filter((t) => !t.finished_at))

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
    fetchTasks,
    fetchTaskLogs,
    sendFollowup,
    finalizeTask,
    cancelTask,
    redirectTask,
    messageTask,
    handleWebSocketMessage,
    clearTasks,
    stateLabel,
    stateColor,
  }
})
