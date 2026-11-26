import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api } from '../utils/api'
import { useUiStore } from './ui'
import { useGuildStore } from './guild'
import type { GitHubIssue, GitHubProject, GitHubRepo } from '../types'

const GH_API = 'https://api.github.com'

function ghHeaders(token: string): Record<string, string> {
  return {
    Authorization: `Bearer ${token}`,
    Accept: 'application/vnd.github.v3+json',
  }
}

// Turns a failed GitHub response into a message the user can act on.
async function ghError(res: Response, what: string): Promise<Error> {
  const limited =
    res.status === 429 || (res.status === 403 && res.headers.get('x-ratelimit-remaining') === '0')
  if (limited) {
    const retryAfter = Number(res.headers.get('retry-after'))
    const reset = Number(res.headers.get('x-ratelimit-reset'))
    const at = retryAfter
      ? new Date(Date.now() + retryAfter * 1000)
      : reset
        ? new Date(reset * 1000)
        : null
    return new Error(
      `GitHub rate limit reached for ${what}; ` +
        (at ? `retry after ${at.toLocaleTimeString()}.` : 'retry in a few minutes.'),
    )
  }
  if (res.status === 403) return new Error(`GitHub denied access to ${what} (403).`)
  if (res.status === 404) return new Error(`${what} was not found or is not accessible (404).`)
  return new Error(`GitHub error ${res.status} for ${what}.`)
}

export interface GitHubComment {
  id: number
  body: string
  user: { login: string; avatar_url: string }
  created_at: string
  updated_at: string
  html_url: string
}

export interface GitHubIssueDetail {
  id: number
  number: number
  title: string
  body: string | null
  state: string
  user: { login: string; avatar_url: string }
  labels: Array<{ id: number; name: string; color: string }>
  created_at: string
  updated_at: string
  html_url: string
  comments: number
}

// Legacy browser-global browsing list (pre-#1453). Only read to offer a one-time
// import into a guild that has never saved its own list.
export function readLegacyRepos(): string[] {
  try {
    const parsed = JSON.parse(localStorage.getItem('gh_repos') || '[]')
    return Array.isArray(parsed) ? parsed : []
  } catch {
    localStorage.removeItem('gh_repos')
    return []
  }
}

export function issueTabId(owner: string, repo: string, number: number): string {
  return `issue-${owner}/${repo}/${number}`
}

export const useGitHubStore = defineStore('github', () => {
  const token = ref<string>(localStorage.getItem('gh_token') || '')
  const guildStore = useGuildStore()
  // Browsing scope is per guild and server-backed, so switching guilds swaps it.
  const selectedRepos = computed(() => guildStore.currentGuild?.browse_repos ?? [])
  const linkedProjects = computed(() => guildStore.currentGuild?.browse_projects ?? [])
  const repos = ref<GitHubRepo[]>([])
  const issues = ref<GitHubIssue[]>([])
  const loading = ref(false)
  const error = ref('')
  const openedIssueKeys = ref<string[]>([])
  let issuesGeneration = 0

  const isConfigured = computed(() => !!token.value)

  function openIssueTab(owner: string, repo: string, number: number) {
    const key = issueTabId(owner, repo, number)
    if (!openedIssueKeys.value.includes(key)) openedIssueKeys.value.push(key)
    useUiStore().activeTab = key
  }

  function closeIssueTab(key: string) {
    const idx = openedIssueKeys.value.indexOf(key)
    if (idx !== -1) openedIssueKeys.value.splice(idx, 1)
    const uiStore = useUiStore()
    if (uiStore.activeTab === key) uiStore.activeTab = 'factory'
  }

  // Called after the OAuth callback redirects back with query params.
  function restoreGitHubToken(params: URLSearchParams) {
    const ghToken = params.get('gh_token')
    if (!ghToken) return false
    token.value = ghToken
    localStorage.setItem('gh_token', ghToken)
    return true
  }

  async function fetchRepos() {
    if (!token.value) return []
    loading.value = true
    try {
      const fetchedRepos = (await fetchAllPages(
        `${GH_API}/user/repos?per_page=100&sort=updated&affiliation=owner,collaborator,organization_member`,
        'your repositories',
      )) as unknown as GitHubRepo[]
      repos.value = fetchedRepos.filter((repo) => !repo.archived)
      return repos.value
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : String(e)
      return []
    } finally {
      loading.value = false
    }
  }

  async function setSelectedRepos(repoFullNames: string[]) {
    const guild = guildStore.currentGuild
    if (!guild) return
    const { browse_repos } = await api<{ browse_repos: string[] }>(
      `/api/guilds/${guild.id}/browse-repos`,
      { method: 'PUT', json: { repos: repoFullNames } },
    )
    if (guildStore.currentGuild?.id === guild.id)
      guildStore.currentGuild.browse_repos = browse_repos
  }

  async function setLinkedProjects(projects: GitHubProject[]) {
    const guild = guildStore.currentGuild
    if (!guild) return
    const { browse_projects } = await api<{ browse_projects: GitHubProject[] }>(
      `/api/guilds/${guild.id}/browse-projects`,
      { method: 'PUT', json: { projects } },
    )
    if (guildStore.currentGuild?.id === guild.id)
      guildStore.currentGuild.browse_projects = browse_projects
  }

  // Lists GitHub Projects linkable to this guild: the authenticated user's, each
  // owner's org/user projects, and each repo's. Classic Projects come from the REST
  // endpoints (GitHub has sunset them, so 404/410 is tolerated); Projects v2 come from
  // GraphQL. `repoNames` are "owner/repo" strings; owners are derived from them.
  async function fetchAvailableProjects(repoNames: string[]): Promise<GitHubProject[]> {
    if (!token.value) return []
    const owners = [...new Set(repoNames.map((r) => r.split('/')[0]!))]
    const out = new Map<string, GitHubProject>()
    const add = (p: GitHubProject) => out.set(p.id, p)
    // A failed or sunset endpoint contributes nothing rather than failing the list.
    const classic = async (url: string, owner: string, kind: GitHubProject['kind']) => {
      try {
        const data = await fetchAllPages(url, 'GitHub projects')
        for (const p of data.filter((project) => project.state === 'open'))
          add({
            id: `classic-${p.id}`,
            name: String(p.name),
            url: String(p.html_url),
            owner,
            kind,
          })
      } catch {
        /* classic projects unavailable for this scope */
      }
    }
    const v2 = async (ownerLogin: string, kind: 'organization' | 'user') => {
      try {
        let after: string | null = null
        // Page through all Projects v2 (GraphQL caps a page at 100 nodes).
        for (let page = 0; page < 10; page++) {
          const res = await fetch(`${GH_API}/graphql`, {
            method: 'POST',
            headers: ghHeaders(token.value),
            body: JSON.stringify({
              query: `query($login:String!,$after:String){${kind}(login:$login){projectsV2(first:100,after:$after){nodes{id title url closed}pageInfo{hasNextPage endCursor}}}}`,
              variables: { login: ownerLogin, after },
            }),
          })
          if (!res.ok) return
          const body = await res.json()
          const conn = body?.data?.[kind]?.projectsV2
          const nodes = conn?.nodes ?? []
          for (const n of nodes as Array<{
            id: string
            title: string
            url: string
            closed: boolean
          }>)
            if (!n.closed)
              add({ id: n.id, name: n.title, url: n.url, owner: ownerLogin, kind: 'v2' })
          if (!conn?.pageInfo?.hasNextPage || !conn.pageInfo.endCursor) return
          after = conn.pageInfo.endCursor
        }
      } catch {
        /* Projects v2 unavailable for this scope */
      }
    }
    let login = ''
    try {
      const res = await fetch(`${GH_API}/user`, { headers: ghHeaders(token.value) })
      if (res.ok) login = (await res.json()).login ?? ''
    } catch {
      /* ignore */
    }
    const tasks: Promise<void>[] = []
    if (login) {
      tasks.push(classic(`${GH_API}/users/${login}/projects?per_page=100`, login, 'user'))
      tasks.push(v2(login, 'user'))
    }
    for (const owner of owners) {
      if (owner === login) continue
      tasks.push(classic(`${GH_API}/orgs/${owner}/projects?per_page=100`, owner, 'org'))
      tasks.push(classic(`${GH_API}/users/${owner}/projects?per_page=100`, owner, 'user'))
      tasks.push(v2(owner, 'organization'))
    }
    for (const repo of repoNames)
      tasks.push(
        classic(`${GH_API}/repos/${repo}/projects?per_page=100`, repo.split('/')[0]!, 'repo'),
      )
    await Promise.all(tasks)
    return [...out.values()]
  }

  async function fetchAllPages(url: string, what: string): Promise<Array<Record<string, unknown>>> {
    const results: Array<Record<string, unknown>> = []
    let nextUrl: string | null = url
    while (nextUrl) {
      const res = await fetch(nextUrl, { headers: ghHeaders(token.value) })
      if (!res.ok) throw await ghError(res, what)
      const data: Array<Record<string, unknown>> = await res.json()
      results.push(...data)
      const link = res.headers.get('Link') || ''
      const m = link.match(/<([^>]+)>;\s*rel="next"/)
      nextUrl = m ? m[1] : null
    }
    return results
  }

  async function fetchIssues(
    repos?: string[],
    silent = false,
    state: 'open' | 'closed' | 'all' = 'all',
  ) {
    const reposToFetch = repos ?? selectedRepos.value
    if (!token.value) return []
    // Latest request wins: anything still in flight is now stale.
    const generation = ++issuesGeneration
    if (reposToFetch.length === 0) {
      issues.value = []
      loading.value = false
      error.value = ''
      return []
    }
    if (!silent) loading.value = true
    try {
      const results = await Promise.allSettled(
        reposToFetch.map(async (repoName) => {
          const data = await fetchAllPages(
            `${GH_API}/repos/${repoName}/issues?state=${state}&per_page=100&sort=created&direction=desc`,
            repoName,
          )
          // PRs are kept: ProjectsView shows them under its Pull requests tab.
          return data.map((i) => ({ ...i, repo: repoName })) as GitHubIssue[]
        }),
      )
      if (generation !== issuesGeneration) return []
      // Failed repos are reported, not shown as empty; successful repos still load.
      error.value = results
        .flatMap((r) => (r.status === 'rejected' ? [String(r.reason?.message ?? r.reason)] : []))
        .join(' ')
      issues.value = results
        .flatMap((r) => (r.status === 'fulfilled' ? r.value : []))
        .sort((a, b) => {
          const priorityLabels = [
            'priority: high',
            'high priority',
            'priority:high',
            'p0',
            'p1',
            'urgent',
            'critical',
          ]
          const aPriority = a.labels.some((l) => priorityLabels.includes(l.name.toLowerCase()))
            ? 0
            : 1
          const bPriority = b.labels.some((l) => priorityLabels.includes(l.name.toLowerCase()))
            ? 0
            : 1
          return (
            aPriority - bPriority ||
            new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
          )
        })
      return issues.value
    } catch (e: unknown) {
      if (generation === issuesGeneration) error.value = e instanceof Error ? e.message : String(e)
      return []
    } finally {
      if (generation === issuesGeneration) loading.value = false
    }
  }

  function clearIssues() {
    issuesGeneration++
    issues.value = []
    openedIssueKeys.value = []
    loading.value = false
    error.value = ''
  }

  async function fetchIssueDetail(
    owner: string,
    repo: string,
    issueNumber: number,
  ): Promise<{ issue: GitHubIssueDetail; comments: GitHubComment[] }> {
    const data = await api<{ issue: GitHubIssueDetail; comments: GitHubComment[] }>(
      `/api/issues/${owner}/${repo}/${issueNumber}`,
    )
    return data
  }

  async function postComment(
    owner: string,
    repo: string,
    issueNumber: number,
    body: string,
  ): Promise<GitHubComment> {
    return api<GitHubComment>(`/api/issues/${owner}/${repo}/${issueNumber}/comments`, {
      method: 'POST',
      json: { body },
    })
  }

  async function updateIssue(
    owner: string,
    repo: string,
    issueNumber: number,
    patch: { title?: string; body?: string },
  ): Promise<GitHubIssueDetail> {
    return api<GitHubIssueDetail>(`/api/issues/${owner}/${repo}/${issueNumber}`, {
      method: 'PATCH',
      json: patch,
    })
  }

  // Issues on a linked Projects v2 board, served from the backend cache (#1513).
  // `sync` asks the backend to refresh from GitHub first.
  async function fetchProjectIssues(projectId: string, sync = false): Promise<GitHubIssue[]> {
    const guild = guildStore.currentGuild
    if (!guild) return []
    const url = `/api/guilds/${guild.id}/projects/${encodeURIComponent(projectId)}/issues`
    const { issues } = await api<{ issues: GitHubIssue[] }>(
      sync ? `${url}/sync` : url,
      sync ? { method: 'POST' } : {},
    )
    return issues
  }

  // Direct children of an issue (GitHub sub-issues), with the user's token.
  async function fetchSubIssues(repo: string, issueNumber: number): Promise<GitHubIssue[]> {
    if (!token.value) return []
    const data = await fetchAllPages(
      `${GH_API}/repos/${repo}/issues/${issueNumber}/sub_issues?per_page=100`,
      `sub-issues of ${repo}#${issueNumber}`,
    )
    return data.map((i) => ({ ...i, repo })) as GitHubIssue[]
  }

  function logout() {
    token.value = ''
    repos.value = []
    clearIssues()
    localStorage.removeItem('gh_token')
    localStorage.removeItem('gh_repos')
  }

  return {
    token,
    repos,
    selectedRepos,
    linkedProjects,
    issues,
    loading,
    error,
    openedIssueKeys,
    isConfigured,
    restoreGitHubToken,
    fetchRepos,
    setSelectedRepos,
    setLinkedProjects,
    fetchAvailableProjects,
    fetchIssues,
    clearIssues,
    fetchIssueDetail,
    fetchProjectIssues,
    fetchSubIssues,
    postComment,
    updateIssue,
    openIssueTab,
    closeIssueTab,
    logout,
  }
})
