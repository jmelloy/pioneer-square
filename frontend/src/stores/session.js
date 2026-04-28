import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useAuthStore } from './auth.js'

const API_BASE = 'http://localhost:8000'

export const useSessionStore = defineStore('session', () => {
  const currentSession = ref(null)
  const sessions = ref([])
  const isConnected = ref(false)
  const messages = ref([])
  let ws = null
  const messageHandlers = ref([])

  function _authHeaders() {
    const authStore = useAuthStore()
    return authStore.authHeaders()
  }

  async function loadSessions() {
    try {
      const res = await fetch(`${API_BASE}/sessions`, { headers: _authHeaders() })
      if (!res.ok) { sessions.value = []; return }
      sessions.value = await res.json()
    } catch (e) {
      console.error('Failed to load sessions', e)
    }
  }

  async function createSession(name) {
    const res = await fetch(`${API_BASE}/sessions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ..._authHeaders() },
      body: JSON.stringify({ name })
    })
    const session = await res.json()
    sessions.value.unshift(session)
    return session
  }

  async function joinSession(sessionId) {
    try {
      const res = await fetch(`${API_BASE}/sessions/${sessionId}`)
      if (!res.ok) throw new Error('Session not found')
      const session = await res.json()
      currentSession.value = session
      messages.value = session.messages || []
      return session
    } catch (e) {
      console.error('Failed to join session', e)
      return null
    }
  }

  function connectWebSocket(sessionId, onMessage, retryCount = 0) {
    const MAX_RETRIES = 10
    if (ws) {
      ws.close()
    }
    ws = new WebSocket(`ws://localhost:8000/ws/${sessionId}`)
    ws.onopen = () => {
      isConnected.value = true
    }
    ws.onmessage = (event) => {
      const data = JSON.parse(event.data)
      if (data.type === 'chat') {
        messages.value.push(data)
      }
      if (onMessage) onMessage(data)
      messageHandlers.value.forEach(h => h(data))
    }
    ws.onclose = () => {
      isConnected.value = false
      if (retryCount < MAX_RETRIES) {
        setTimeout(() => connectWebSocket(sessionId, onMessage, retryCount + 1), 2000)
      }
    }
    ws.onerror = () => {
      isConnected.value = false
    }
    return ws
  }

  function sendMessage(data) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(data))
    }
  }

  function addMessageHandler(handler) {
    messageHandlers.value.push(handler)
  }

  function removeMessageHandler(handler) {
    messageHandlers.value = messageHandlers.value.filter(h => h !== handler)
  }

  return {
    currentSession,
    sessions,
    isConnected,
    messages,
    loadSessions,
    createSession,
    joinSession,
    connectWebSocket,
    sendMessage,
    addMessageHandler,
    removeMessageHandler
  }
})
