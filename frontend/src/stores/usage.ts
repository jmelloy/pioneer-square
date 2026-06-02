import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '../utils/api'
import type { ClaudeUsageWS, WSInbound } from '../types'

// Per-task usage summary — the shape broadcast over WS, also what we aggregate
// REST rows into so the UI has a single type to render.
export type UsageSummary = Omit<ClaudeUsageWS, 'type'>

// One raw row as stored in claude_usage (per API call or the run's result).
interface UsageRow {
  task_id: string | null
  worker_id: string | null
  session_id: string | null
  kind: string
  call_index: number | null
  model: string | null
  input_tokens: number
  output_tokens: number
  cache_read_input_tokens: number
  cache_creation_input_tokens: number
  cost_usd: number | null
  num_turns: number | null
  stop_reason: string | null
  repo: string | null
  reporter: string | null
}

function _emptySummary(taskId: string | null): UsageSummary {
  return {
    taskId,
    apiCalls: 0,
    summedInputTokens: 0,
    summedOutputTokens: 0,
    summedCacheReadTokens: 0,
    summedCacheCreationTokens: 0,
    reportedInputTokens: null,
    reportedOutputTokens: null,
    reportedCacheReadTokens: null,
    reportedCacheCreationTokens: null,
    costUsd: null,
    numTurns: null,
    stopReason: null,
  }
}

function _aggregate(rows: UsageRow[]): Record<string, UsageSummary> {
  const out: Record<string, UsageSummary> = {}
  for (const r of rows) {
    if (!r.task_id) continue
    const s = (out[r.task_id] ??= _emptySummary(r.task_id))
    s.model ??= r.model
    s.repo ??= r.repo
    s.reporter ??= r.reporter
    s.workerId ??= r.worker_id
    s.sessionId ??= r.session_id
    if (r.kind === 'api_call') {
      s.apiCalls += 1
      s.summedInputTokens += r.input_tokens
      s.summedOutputTokens += r.output_tokens
      s.summedCacheReadTokens += r.cache_read_input_tokens
      s.summedCacheCreationTokens += r.cache_creation_input_tokens
    } else if (r.kind === 'result') {
      s.reportedInputTokens = r.input_tokens
      s.reportedOutputTokens = r.output_tokens
      s.reportedCacheReadTokens = r.cache_read_input_tokens
      s.reportedCacheCreationTokens = r.cache_creation_input_tokens
      s.costUsd = r.cost_usd
      s.numTurns = r.num_turns
      s.stopReason = r.stop_reason
    }
  }
  return out
}

export const useUsageStore = defineStore('usage', () => {
  // Latest usage summary keyed by task id.
  const byTask = ref<Record<string, UsageSummary>>({})

  const summaries = computed(() => Object.values(byTask.value))

  // Guild-wide totals across every reported task.
  const totals = computed(() => {
    const acc = { apiCalls: 0, inputTokens: 0, outputTokens: 0, costUsd: 0 }
    for (const s of Object.values(byTask.value)) {
      acc.apiCalls += s.apiCalls
      acc.inputTokens += s.summedInputTokens
      acc.outputTokens += s.summedOutputTokens
      acc.costUsd += s.costUsd ?? 0
    }
    return acc
  })

  async function fetchUsage(guildId: string) {
    if (!guildId) return
    try {
      const rows = await api<UsageRow[]>(`/guilds/${guildId}/usage`)
      byTask.value = _aggregate(rows)
    } catch (e) {
      console.error('Failed to fetch usage', e)
    }
  }

  function handleWebSocketMessage(data: WSInbound) {
    switch (data.type) {
      case 'claude-usage': {
        if (!data.taskId) break
        const { type: _t, ...summary } = data
        byTask.value = { ...byTask.value, [data.taskId]: summary }
        break
      }
      default:
        break
    }
  }

  function usageForTask(taskId: string): UsageSummary | null {
    return byTask.value[taskId] ?? null
  }

  function clearUsage() {
    byTask.value = {}
  }

  return {
    byTask,
    summaries,
    totals,
    fetchUsage,
    handleWebSocketMessage,
    usageForTask,
    clearUsage,
  }
})
