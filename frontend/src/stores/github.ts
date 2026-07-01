import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api } from '../utils/api'
import type { GitHubIssue, GitHubRepo } from '../types'

const GH_API = 'https://api.github.com'

function ghHeaders(token: string): Record<string, string> {
  return {
    Authorization: `Bearer ${token}`,
    Accept: 'application/vnd.github.v3+json',
  }
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

function _readStoredRepos(): string[] {
  try {
    const parsed = JSON.parse(localStorage.getItem('gh_repos') || '[]')
    return Array.isArray(parsed) ? parsed : []
  } catch {
    localStorage.removeItem('gh_repos')
    return []
  }
}

export const useGitHubStore = defineStore('github', () => {
  const token = ref<string>(localStorage.getItem('gh_token') || '')
  const selectedRepos = ref<string[]>(_readStoredRepos())
  const repos = ref<GitHubRepo[]>([])
  const issues = ref<GitHubIssue[]>([])
  const loading = ref(false)
  const error = ref('')

  const isConfigured = computed(() => !!token.value)

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
      const res = await fetch(
        `${GH_API}/user/repos?per_page=100&sort=updated&affiliation=owner,collaborator,organization_member`,
        {
          headers: ghHeaders(token.value),
        },
      )
      if (!res.ok) throw new Error(`GitHub error ${res.status}`)
      repos.value = await res.json()
      return repos.value
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : String(e)
      return []
    } finally {
      loading.value = false
    }
  }

  function setSelectedRepos(repoFullNames: string[]) {
    selectedRepos.value = repoFullNames
    localStorage.setItem('gh_repos', JSON.stringify(repoFullNames))
  }

  async function fetchAllPages(url: string): Promise<Array<Record<string, unknown>>> {
    const results: Array<Record<string, unknown>> = []
    let nextUrl: string | null = url
    while (nextUrl) {
      const res = await fetch(nextUrl, { headers: ghHeaders(token.value) })
      if (!res.ok) break
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
    if (!token.value || reposToFetch.length === 0) return []
    if (!silent) loading.value = true
    try {
      const allIssues = await Promise.all(
        reposToFetch.map(async (repoName) => {
          const data = await fetchAllPages(
            `${GH_API}/repos/${repoName}/issues?state=${state}&per_page=100&sort=created&direction=desc`,
          )
          return data
            .filter((i) => !i.pull_request)
            .map((i) => ({ ...i, repo: repoName })) as GitHubIssue[]
        }),
      )
      issues.value = allIssues.flat().sort((a, b) => {
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
      error.value = e instanceof Error ? e.message : String(e)
      return []
    } finally {
      if (!silent) loading.value = false
    }
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

  function logout() {
    token.value = ''
    selectedRepos.value = []
    repos.value = []
    issues.value = []
    error.value = ''
    localStorage.removeItem('gh_token')
    localStorage.removeItem('gh_repos')
  }

  return {
    token,
    repos,
    selectedRepos,
    issues,
    loading,
    error,
    isConfigured,
    restoreGitHubToken,
    fetchRepos,
    setSelectedRepos,
    fetchIssues,
    fetchIssueDetail,
    postComment,
    updateIssue,
    logout,
  }
})
