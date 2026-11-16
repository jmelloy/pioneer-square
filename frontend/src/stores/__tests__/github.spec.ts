import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useGitHubStore } from '../github'

describe('useGitHubStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('excludes archived repos from fetched choices without removing existing selections', async () => {
    const active = { full_name: 'dnsid-ai/server', archived: false }
    const archived = { full_name: 'dnsid-ai/old-server', archived: true }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json([active, archived])))
    const store = useGitHubStore()
    store.token = 'test-token'
    store.selectedRepos = [archived.full_name]

    expect(await store.fetchRepos()).toEqual([active])
    expect(store.repos).toEqual([active])
    expect(store.selectedRepos).toEqual([archived.full_name])
  })
})
