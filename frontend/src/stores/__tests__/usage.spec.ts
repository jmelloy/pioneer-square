import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useUsageStore } from '../usage'
import type { ClaudeUsageWS } from '../../types'

function wsEvent(over: Partial<ClaudeUsageWS> = {}): ClaudeUsageWS {
  return {
    type: 'claude-usage',
    taskId: 't-1',
    apiCalls: 2,
    summedInputTokens: 8,
    summedOutputTokens: 11,
    summedCacheReadTokens: 1,
    summedCacheCreationTokens: 2,
    reportedInputTokens: 100,
    reportedOutputTokens: 11,
    reportedCacheReadTokens: 0,
    reportedCacheCreationTokens: 0,
    costUsd: 0.5,
    numTurns: 2,
    stopReason: 'success',
    model: 'claude-opus-4-8',
    ...over,
  }
}

describe('useUsageStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  describe('handleWebSocketMessage', () => {
    it('records a claude-usage summary keyed by task', () => {
      const store = useUsageStore()
      store.handleWebSocketMessage(wsEvent())
      const u = store.usageForTask('t-1')
      expect(u).not.toBeNull()
      expect(u?.apiCalls).toBe(2)
      expect(u?.summedInputTokens).toBe(8)
      expect(u?.reportedInputTokens).toBe(100)
      expect(u?.costUsd).toBe(0.5)
    })

    it('latest event replaces the prior summary for a task', () => {
      const store = useUsageStore()
      store.handleWebSocketMessage(wsEvent({ apiCalls: 1 }))
      store.handleWebSocketMessage(wsEvent({ apiCalls: 5 }))
      expect(store.usageForTask('t-1')?.apiCalls).toBe(5)
    })

    it('ignores non-usage messages and usage without a task id', () => {
      const store = useUsageStore()
      store.handleWebSocketMessage({ type: 'task-followup-done', taskId: 't-1' })
      store.handleWebSocketMessage(wsEvent({ taskId: null }))
      expect(store.summaries).toHaveLength(0)
    })
  })

  describe('totals', () => {
    it('sums api calls, tokens and cost across tasks', () => {
      const store = useUsageStore()
      store.handleWebSocketMessage(wsEvent({ taskId: 't-1', apiCalls: 2, costUsd: 0.5 }))
      store.handleWebSocketMessage(
        wsEvent({ taskId: 't-2', apiCalls: 3, summedInputTokens: 10, costUsd: 1.25 }),
      )
      expect(store.totals.apiCalls).toBe(5)
      expect(store.totals.inputTokens).toBe(18)
      expect(store.totals.costUsd).toBeCloseTo(1.75)
    })
  })

  describe('fetchUsage', () => {
    it('aggregates per-call and result rows into a task summary', async () => {
      const rows = [
        { task_id: 't-9', kind: 'api_call', input_tokens: 5, output_tokens: 7,
          cache_read_input_tokens: 1, cache_creation_input_tokens: 2, model: 'claude-opus-4-8',
          cost_usd: null, num_turns: null, stop_reason: null, worker_id: 'w-1', session_id: 's',
          call_index: 0, repo: 'o/r', reporter: 'x' },
        { task_id: 't-9', kind: 'api_call', input_tokens: 3, output_tokens: 4,
          cache_read_input_tokens: 0, cache_creation_input_tokens: 0, model: 'claude-opus-4-8',
          cost_usd: null, num_turns: null, stop_reason: null, worker_id: 'w-1', session_id: 's',
          call_index: 1, repo: 'o/r', reporter: 'x' },
        { task_id: 't-9', kind: 'result', input_tokens: 100, output_tokens: 11,
          cache_read_input_tokens: 0, cache_creation_input_tokens: 0, model: 'claude-opus-4-8',
          cost_usd: 0.5, num_turns: 2, stop_reason: 'success', worker_id: 'w-1', session_id: 's',
          call_index: null, repo: 'o/r', reporter: 'x' },
      ]
      vi.stubGlobal(
        'fetch',
        vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(rows) }),
      )
      const store = useUsageStore()
      await store.fetchUsage('g-1')
      const u = store.usageForTask('t-9')
      expect(u?.apiCalls).toBe(2)
      expect(u?.summedInputTokens).toBe(8)
      expect(u?.summedOutputTokens).toBe(11)
      expect(u?.reportedInputTokens).toBe(100)
      expect(u?.costUsd).toBe(0.5)
      expect(u?.numTurns).toBe(2)
    })

    it('no-ops on empty guild id', async () => {
      const fetchMock = vi.fn()
      vi.stubGlobal('fetch', fetchMock)
      const store = useUsageStore()
      await store.fetchUsage('')
      expect(fetchMock).not.toHaveBeenCalled()
    })
  })

  describe('clearUsage', () => {
    it('drops all summaries', () => {
      const store = useUsageStore()
      store.handleWebSocketMessage(wsEvent())
      store.clearUsage()
      expect(store.summaries).toHaveLength(0)
    })
  })
})
