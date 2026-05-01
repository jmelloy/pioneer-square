import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { AuthUser } from '../types'

const API_BASE = (import.meta.env.VITE_API_BASE as string) ?? ''

export const useAuthStore = defineStore('auth', () => {
  const loginToken = ref<string>(localStorage.getItem('auth_token') || '')
  const user = ref<AuthUser | null>(JSON.parse(localStorage.getItem('auth_user') || 'null'))

  const isLoggedIn = computed(() => !!loginToken.value && !!user.value)

  function authHeaders(): Record<string, string> {
    return loginToken.value ? { Authorization: `Bearer ${loginToken.value}` } : {}
  }

  async function loginWithGitHub() {
    const res = await fetch(`${API_BASE}/auth/github/login`)
    if (!res.ok) {
      const body = await res.json().catch(() => ({}))
      throw new Error(body.detail || `Server error ${res.status}`)
    }
    const { url } = await res.json()
    window.location.href = url
  }

  // Called on landing page mount when GitHub redirects back with query params.
  // Returns true if callback params were present and processed.
  function restoreFromCallback(params: URLSearchParams) {
    const token = params.get('login_token')
    const login = params.get('gh_login')
    const name = params.get('gh_name')
    const avatar = params.get('gh_avatar')
    const userId = params.get('gh_user_id')
    if (!token || !login) return false

    loginToken.value = token
    user.value = { id: userId, login, name: name || login, avatar_url: avatar || '' }
    localStorage.setItem('auth_token', token)
    localStorage.setItem('auth_user', JSON.stringify(user.value))
    return true
  }

  // Exchange an OAuth code+state with the backend and persist the resulting session.
  async function exchangeCode(code: string, state: string): Promise<{ gh_token: string } | null> {
    const res = await fetch(`${API_BASE}/auth/github/exchange`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code, state }),
    })
    if (!res.ok) {
      const body = await res.json().catch(() => ({}))
      throw new Error(body.detail || `Auth exchange failed: ${res.status}`)
    }
    const data = await res.json()
    const params = new URLSearchParams(data)
    restoreFromCallback(params)
    return data
  }

  async function logout() {
    if (loginToken.value) {
      await fetch(`${API_BASE}/auth/logout`, {
        method: 'DELETE',
        headers: authHeaders(),
      }).catch(() => {})
    }
    loginToken.value = ''
    user.value = null
    localStorage.removeItem('auth_token')
    localStorage.removeItem('auth_user')
  }

  return {
    loginToken,
    user,
    isLoggedIn,
    authHeaders,
    loginWithGitHub,
    restoreFromCallback,
    exchangeCode,
    logout,
  }
})
