import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useApiLogStore } from '../apiLog'

describe('apiLog store', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('tracks pending, ok and failed calls', () => {
    const log = useApiLogStore()
    const a = log.start('GET', '/guilds?x=1')
    const b = log.start('GET', '/guilds')
    expect(log.pendingCount).toBe(2)
    log.finish(a, { ok: true, httpStatus: 200 })
    log.finish(b, { ok: false, httpStatus: 500 })
    expect(log.pendingCount).toBe(0)
    expect(log.failedCount).toBe(1)
    expect(log.endpoints).toEqual([{ key: 'GET /guilds', count: 2, failed: 1 }])
    expect(log.avgDurationMs).not.toBeNull()
  })

  it('caps the buffer and clears', () => {
    const log = useApiLogStore()
    for (let i = 0; i < 250; i++) log.start('GET', `/p/${i}`)
    expect(log.calls.length).toBe(200)
    log.clear()
    expect(log.calls).toEqual([])
  })
})
