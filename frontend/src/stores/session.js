import { defineStore } from 'pinia'
import { ref } from 'vue'

const API_BASE = 'http://localhost:8000'

export const useSessionStore = defineStore('session', () => {
  const currentSession = ref(null)
  const sessions = ref([])
  const isConnected = ref(false)
  const messages = ref([])
  let ws = null
  const messageHandlers = ref([])

  async function loadSessions() {
    try {
      const res = await fetch(`${API_BASE}/sessions`)
      sessions.value = await res.json()
    } catch (e) {
      console.error('Failed to load sessions', e)
    }
  }

  async function createSession(name) {
    const res = await fetch(`${API_BASE}/sessions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
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

  function connectWebSocket(sessionId, onMessage) {
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
      // Reconnect after 2s
      setTimeout(() => connectWebSocket(sessionId, onMessage), 2000)
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
