import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useGitHubStore } from '../github'
import { useGuildStore } from '../guild'

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
    useGuildStore().currentGuild = { id: 'g1', browse_repos: [archived.full_name] }

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

    it('uses the backend list endpoint instead of calling GitHub', async () => {
      const fetchMock = vi.fn().mockResolvedValue(Response.json([gh(1)]))
      vi.stubGlobal('fetch', fetchMock)
      const store = useGitHubStore()
      store.token = 't'
      await store.fetchIssues(['team/a'], false, 'closed')
      expect(fetchMock).toHaveBeenCalledOnce()
      expect(fetchMock.mock.calls[0]![0]).toBe('/api/issues/team/a?state=closed')
      expect(store.issues[0]?.repo).toBe('team/a')
    })

    it.each([
      [403, 'GitHub denied access to team/a (403).'],
      [404, 'team/a was not found or is not accessible (404).'],
      [429, 'GitHub rate limit reached for team/a; retry in a few minutes.'],
    ])('surfaces %i failures while keeping other repos', async (status, message) => {
      vi.stubGlobal(
        'fetch',
        vi.fn(async (url: string) =>
          url.includes('team/a')
            ? Response.json({ detail: message }, { status })
            : Response.json([gh(1)]),
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

  describe('fetchAvailableProjects', () => {
    function mockGh(handler: (url: string, init?: RequestInit) => Response | undefined) {
      const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
        const url = String(input)
        if (url.endsWith('/user')) return Response.json({ login: 'me' })
        return handler(url, init) ?? Response.json([])
      })
      vi.stubGlobal('fetch', fetchMock)
      const store = useGitHubStore()
      store.token = 't'
      return store
    }

    it('merges repo projects with the parent org projects', async () => {
      const store = mockGh((url) => {
        if (url.includes('/orgs/team/projects'))
          return Response.json([
            { id: 1, name: 'Org board', html_url: 'https://x/1', state: 'open' },
          ])
        if (url.includes('/repos/team/a/projects'))
          return Response.json([
            { id: 2, name: 'Repo board', html_url: 'https://x/2', state: 'open' },
          ])
        if (url.includes('/repos/team/b/projects'))
          return Response.json([
            { id: 2, name: 'Repo board', html_url: 'https://x/2', state: 'open' },
          ])
      })
      const out = await store.fetchAvailableProjects(['team/a', 'team/b'])
      expect(out.map((p) => [p.name, p.kind]).sort()).toEqual([
        ['Org board', 'org'],
        ['Repo board', 'repo'],
      ])
    })

    it('excludes closed classic and Projects v2 projects', async () => {
      const store = mockGh((url, init) => {
        if (url.includes('/repos/team/a/projects'))
          return Response.json([
            { id: 1, name: 'Active', html_url: 'a', state: 'open' },
            { id: 2, name: 'Completed', html_url: 'b', state: 'closed' },
          ])
        if (url.endsWith('/graphql')) {
          const { variables } = JSON.parse(init!.body as string)
          if (variables.login !== 'me') return Response.json({ data: {} })
          return Response.json({
            data: {
              user: {
                projectsV2: {
                  nodes: [
                    { id: 'v1', title: 'In progress', url: 'c', closed: false },
                    { id: 'v2', title: 'Archived', url: 'd', closed: true },
                  ],
                },
              },
            },
          })
        }
      })
      expect((await store.fetchAvailableProjects(['team/a'])).map((p) => p.name).sort()).toEqual([
        'Active',
        'In progress',
      ])
    })

    it('tolerates an owner with no org projects (404)', async () => {
      const store = mockGh((url) => {
        if (url.includes('/orgs/')) return Response.json({ message: 'Not Found' }, { status: 404 })
        if (url.includes('/repos/team/a/projects'))
          return Response.json([
            { id: 2, name: 'Repo board', html_url: 'https://x/2', state: 'open' },
          ])
      })
      const out = await store.fetchAvailableProjects(['team/a'])
      expect(out.map((p) => p.name)).toEqual(['Repo board'])
    })

    it('follows pagination for org projects and Projects v2', async () => {
      const node = (n: number) => ({ id: `v2-${n}`, title: `P${n}`, url: `u${n}`, closed: false })
      const store = mockGh((url, init) => {
        if (url.endsWith('/graphql')) {
          const { variables, query } = JSON.parse(init!.body as string)
          if (variables.login !== 'team' || !query.includes('organization('))
            return Response.json({ data: {} })
          const second = variables.after === 'c1'
          return Response.json({
            data: {
              organization: {
                projectsV2: {
                  nodes: [node(second ? 2 : 1)],
                  pageInfo: { hasNextPage: !second, endCursor: 'c1' },
                },
              },
            },
          })
        }
        if (url.includes('/orgs/team/projects?per_page=100'))
          return new Response(
            JSON.stringify([{ id: 1, name: 'O1', html_url: 'a', state: 'open' }]),
            {
              headers: { Link: '<https://api.github.com/orgs/team/projects?page=2>; rel="next"' },
            },
          )
        if (url.includes('/orgs/team/projects?page=2'))
          return Response.json([{ id: 2, name: 'O2', html_url: 'b', state: 'open' }])
      })
      const names = (await store.fetchAvailableProjects(['team/a'])).map((p) => p.name).sort()
      expect(names).toEqual(['O1', 'O2', 'P1', 'P2'])
    })
  })

  it('scopes the browsing list to the current guild', () => {
    const store = useGitHubStore()
    const guildStore = useGuildStore()
    localStorage.setItem('gh_repos', JSON.stringify(['legacy/repo']))
    expect(store.selectedRepos).toEqual([]) // legacy browser-global list is not the scope
    guildStore.currentGuild = { id: 'a', browse_repos: ['a/one'] }
    expect(store.selectedRepos).toEqual(['a/one'])
    guildStore.currentGuild = { id: 'b', browse_repos: null }
    expect(store.selectedRepos).toEqual([])
    localStorage.clear()
  })
})
