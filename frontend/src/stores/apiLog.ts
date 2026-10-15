import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

export type ApiCallStatus = 'pending' | 'ok' | 'failed'

export interface ApiCall {
  id: number
  method: string
  path: string
  status: ApiCallStatus
  httpStatus: number | null
  startedAt: number
  durationMs: number | null
  error: string | null
}

const MAX_ENTRIES = 200

// Ring buffer of recent REST calls made through utils/api.ts, surfaced by ApiDebugSidebar.
export const useApiLogStore = defineStore('apiLog', () => {
  const calls = ref<ApiCall[]>([])
  let nextId = 1

  function start(method: string, path: string): number {
    const id = nextId++
    calls.value.unshift({
      id,
      method,
      path,
      status: 'pending',
      httpStatus: null,
      startedAt: Date.now(),
      durationMs: null,
      error: null,
    })
    if (calls.value.length > MAX_ENTRIES) calls.value.length = MAX_ENTRIES
    return id
  }

  function finish(id: number, result: { httpStatus?: number; ok: boolean; error?: string }) {
    const call = calls.value.find((c) => c.id === id)
    if (!call) return
    call.status = result.ok ? 'ok' : 'failed'
    call.httpStatus = result.httpStatus ?? null
    call.durationMs = Date.now() - call.startedAt
    call.error = result.error ?? null
  }

  function clear() {
    calls.value = []
  }

  const pendingCount = computed(() => calls.value.filter((c) => c.status === 'pending').length)
  const failedCount = computed(() => calls.value.filter((c) => c.status === 'failed').length)
  const avgDurationMs = computed(() => {
    const done = calls.value.filter((c) => c.durationMs !== null)
    if (!done.length) return null
    return Math.round(done.reduce((s, c) => s + (c.durationMs ?? 0), 0) / done.length)
  })

  // Distinct method+path (query string stripped) with call counts, for the endpoint summary.
  const endpoints = computed(() => {
    const map = new Map<string, { key: string; count: number; failed: number }>()
    for (const c of calls.value) {
      const key = `${c.method} ${c.path.split('?')[0]}`
      const e = map.get(key) ?? { key, count: 0, failed: 0 }
      e.count++
      if (c.status === 'failed') e.failed++
      map.set(key, e)
    }
    return [...map.values()].sort((a, b) => b.count - a.count)
  })

  return { calls, start, finish, clear, pendingCount, failedCount, avgDurationMs, endpoints }
})
