import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { GitHubIssue, GitHubRepo } from '../types'

const GH_API = 'https://api.github.com'

function ghHeaders(token: string): Record<string, string> {
  return {
    Authorization: `Bearer ${token}`,
    Accept: 'application/vnd.github.v3+json',
  }
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
        `${GH_API}/user/repos?per_page=100&sort=updated&affiliation=owner,collaborator`,
        {
          headers: ghHeaders(token.value),
        },
      )
      if (!res.ok) throw new Error(`GitHub error ${res.status}`)
      repos.value = await res.json()
      return repos.value
    } catch (e: any) {
      error.value = e.message
      return []
    } finally {
      loading.value = false
    }
  }

  function setSelectedRepos(repoFullNames: string[]) {
    selectedRepos.value = repoFullNames
    localStorage.setItem('gh_repos', JSON.stringify(repoFullNames))
  }

  async function fetchIssues(repos?: string[], silent = false) {
    const reposToFetch = repos ?? selectedRepos.value
    if (!token.value || reposToFetch.length === 0) return []
    if (!silent) loading.value = true
    try {
      const allIssues = await Promise.all(
        reposToFetch.map(async (repoName) => {
          const res = await fetch(
            `${GH_API}/repos/${repoName}/issues?state=open&per_page=30&sort=created&direction=desc`,
            { headers: ghHeaders(token.value) },
          )
          if (!res.ok) return [] as GitHubIssue[]
          const data = await res.json()
          return data
            .filter((i: any) => !i.pull_request)
            .map((i: any) => ({ ...i, repo: repoName })) as GitHubIssue[]
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
    } catch (e: any) {
      error.value = e.message
      return []
    } finally {
      if (!silent) loading.value = false
    }
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
    logout,
  }
})
