import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

const GH_API = 'https://api.github.com'

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

  async function authenticate(newToken) {
    error.value = ''
    loading.value = true
    try {
      const res = await fetch(`${GH_API}/user`, { headers: ghHeaders(newToken) })
      if (!res.ok) throw new Error(res.status === 401 ? 'Invalid token' : `GitHub error ${res.status}`)
      const userData = await res.json()
      token.value = newToken
      user.value = userData
      localStorage.setItem('gh_token', newToken)
      localStorage.setItem('gh_user', JSON.stringify(userData))
      return { success: true, user: userData }
    } catch (e) {
      error.value = e.message
      return { success: false, error: e.message }
    } finally {
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
    authenticate,
    fetchRepos,
    setSelectedRepos,
    fetchIssues,
    logout,
  }
})
