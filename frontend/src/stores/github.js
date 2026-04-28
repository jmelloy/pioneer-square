import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

const GH_API = 'https://api.github.com'
const API_BASE = 'http://localhost:8000'

function ghHeaders(token) {
  return {
    Authorization: `Bearer ${token}`,
    Accept: 'application/vnd.github.v3+json',
  }
}

export const useGitHubStore = defineStore('github', () => {
  const token = ref(localStorage.getItem('gh_token') || '')
  const user = ref(JSON.parse(localStorage.getItem('gh_user') || 'null'))
  const selectedRepos = ref(JSON.parse(localStorage.getItem('gh_repos') || '[]'))
  const repos = ref([])
  const issues = ref([])
  const loading = ref(false)
  const error = ref('')

  const isConfigured = computed(() => !!token.value && !!user.value)

  // Called after the OAuth callback redirects back with query params
  function restoreFromOAuthCallback(params) {
    const ghToken = params.get('gh_token')
    const ghLogin = params.get('gh_login')
    const ghName = params.get('gh_name')
    const ghAvatar = params.get('gh_avatar')
    const ghUserId = params.get('gh_user_id')
    if (!ghToken || !ghLogin) return false

    const userData = {
      id: ghUserId,
      login: ghLogin,
      name: ghName || ghLogin,
      avatar_url: ghAvatar || '',
    }
    token.value = ghToken
    user.value = userData
    localStorage.setItem('gh_token', ghToken)
    localStorage.setItem('gh_user', JSON.stringify(userData))
    return true
  }

  // Redirect to GitHub OAuth. sessionId is used by the backend to link the token.
  async function loginWithOAuth(sessionId) {
    error.value = ''
    loading.value = true
    try {
      const res = await fetch(`${API_BASE}/auth/github/login?session_id=${sessionId}`)
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || `Server error ${res.status}`)
      }
      const { url } = await res.json()
      window.location.href = url
    } catch (e) {
      error.value = e.message
      loading.value = false
    }
  }

  async function fetchRepos() {
    if (!token.value) return []
    loading.value = true
    try {
      const res = await fetch(`${GH_API}/user/repos?per_page=100&sort=updated&affiliation=owner,collaborator`, {
        headers: ghHeaders(token.value),
      })
      if (!res.ok) throw new Error(`GitHub error ${res.status}`)
      repos.value = await res.json()
      return repos.value
    } catch (e) {
      error.value = e.message
      return []
    } finally {
      loading.value = false
    }
  }

  function setSelectedRepos(repoFullNames) {
    selectedRepos.value = repoFullNames
    localStorage.setItem('gh_repos', JSON.stringify(repoFullNames))
  }

  async function fetchIssues() {
    if (!token.value || selectedRepos.value.length === 0) return []
    loading.value = true
    try {
      const allIssues = await Promise.all(
        selectedRepos.value.map(async (repoName) => {
          const res = await fetch(
            `${GH_API}/repos/${repoName}/issues?state=open&per_page=30&sort=created&direction=desc`,
            { headers: ghHeaders(token.value) }
          )
          if (!res.ok) return []
          const data = await res.json()
          return data
            .filter(i => !i.pull_request)
            .map(i => ({ ...i, repo: repoName }))
        })
      )
      issues.value = allIssues.flat().sort((a, b) => {
        const priorityLabels = ['priority: high', 'high priority', 'priority:high', 'p0', 'p1', 'urgent', 'critical']
        const aPriority = a.labels.some(l => priorityLabels.includes(l.name.toLowerCase())) ? 0 : 1
        const bPriority = b.labels.some(l => priorityLabels.includes(l.name.toLowerCase())) ? 0 : 1
        return aPriority - bPriority || new Date(b.created_at) - new Date(a.created_at)
      })
      return issues.value
    } catch (e) {
      error.value = e.message
      return []
    } finally {
      loading.value = false
    }
  }

  function logout() {
    token.value = ''
    user.value = null
    selectedRepos.value = []
    repos.value = []
    issues.value = []
    error.value = ''
    localStorage.removeItem('gh_token')
    localStorage.removeItem('gh_user')
    localStorage.removeItem('gh_repos')
  }

  return {
    token,
    user,
    repos,
    selectedRepos,
    issues,
    loading,
    error,
    isConfigured,
    loginWithOAuth,
    restoreFromOAuthCallback,
    fetchRepos,
    setSelectedRepos,
    fetchIssues,
    logout,
  }
})
