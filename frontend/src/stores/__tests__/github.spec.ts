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

  describe('fetchIssues', () => {
    const gh = (id: number) => ({
      id,
      number: id,
      title: `#${id}`,
      labels: [],
      created_at: '2026-01-01',
    })
    function deferred() {
      let resolve!: (r: Response) => void
      return { promise: new Promise<Response>((r) => (resolve = r)), resolve }
    }

    it('ignores a slower, older request that resolves after a newer one', async () => {
      const slow = deferred()
      const fetchMock = vi
        .fn()
        .mockReturnValueOnce(slow.promise)
        .mockResolvedValueOnce(Response.json([gh(2)]))
      vi.stubGlobal('fetch', fetchMock)
      const store = useGitHubStore()
      store.token = 't'
      const old = store.fetchIssues(['team/old'])
      await store.fetchIssues(['team/new'])
      slow.resolve(Response.json([gh(1)]))
      expect(await old).toEqual([])
      expect(store.issues.map((i) => i.repo)).toEqual(['team/new'])
      expect(store.loading).toBe(false)
    })

    it('clears issues for an empty list and drops in-flight results', async () => {
      const slow = deferred()
      vi.stubGlobal('fetch', vi.fn().mockReturnValue(slow.promise))
      const store = useGitHubStore()
      store.token = 't'
      store.issues = [{ ...gh(9), repo: 'team/x' } as never]
      const pending = store.fetchIssues(['team/x'])
      await store.fetchIssues([])
      expect(store.issues).toEqual([])
      slow.resolve(Response.json([gh(1)]))
      await pending
      expect(store.issues).toEqual([])
    })

    it.each([
      [403, {}, 'denied access to team/a'],
      [404, {}, 'team/a was not found'],
      [429, { 'retry-after': '60' }, 'rate limit reached for team/a; retry after'],
      [403, { 'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '2000000000' }, 'rate limit'],
    ])('surfaces %i failures while keeping other repos', async (status, headers, message) => {
      vi.stubGlobal(
        'fetch',
        vi.fn(async (url: string) =>
          url.includes('team/a') ? new Response('{}', { status, headers }) : Response.json([gh(1)]),
        ),
      )
      const store = useGitHubStore()
      store.token = 't'
      await store.fetchIssues(['team/a', 'team/b'])
      expect(store.error).toContain(message)
      expect(store.issues.map((i) => i.repo)).toEqual(['team/b'])
      // A later successful refresh clears the error.
      await store.fetchIssues(['team/b'])
      expect(store.error).toBe('')
    })
  })

  it('follows pagination when listing repositories', async () => {
    const page2 = 'https://api.github.com/user/repos?page=2'
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        Response.json(
          Array.from({ length: 100 }, (_, i) => ({ full_name: `o/r${i}`, archived: false })),
          { headers: { Link: `<${page2}>; rel="next"` } },
        ),
      )
      .mockResolvedValueOnce(Response.json([{ full_name: 'o/last', archived: false }]))
    vi.stubGlobal('fetch', fetchMock)
    const store = useGitHubStore()
    store.token = 't'
    const repos = await store.fetchRepos()
    expect(repos).toHaveLength(101)
    expect(fetchMock).toHaveBeenLastCalledWith(page2, expect.anything())
  })

  it('reports repository listing failures instead of an empty list', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 403 })))
    const store = useGitHubStore()
    store.token = 't'
    expect(await store.fetchRepos()).toEqual([])
    expect(store.error).toContain('denied access to your repositories')
  })
})
