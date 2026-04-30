import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useAuthStore } from './auth'
import type { ChatMessage, Guild, WSMessage } from '../types'

const API_BASE = 'http://localhost:8000'

type MessageHandler = (data: WSMessage) => void

export const useGuildStore = defineStore('guild', () => {
  const currentGuild = ref<Guild | null>(null)
  const guilds = ref<Guild[]>([])
  const isConnected = ref(false)
  const messages = ref<ChatMessage[]>([])
  const reconnectAttempt = ref(0)
  let ws: WebSocket | null = null
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let manualClose = false
  const messageHandlers = ref<MessageHandler[]>([])

  const MAX_RETRIES = 10
  const BASE_DELAY_MS = 1000
  const MAX_DELAY_MS = 30000

  function _backoffDelay(attempt: number): number {
    const exp = Math.min(MAX_DELAY_MS, BASE_DELAY_MS * 2 ** attempt)
    // Full jitter: random value in [0, exp]
    return Math.floor(Math.random() * exp)
  }

  function _authHeaders(): Record<string, string> {
    const authStore = useAuthStore()
    return authStore.authHeaders()
  }

  async function loadGuilds() {
    try {
      const res = await fetch(`${API_BASE}/guilds`, { headers: _authHeaders() })
      if (!res.ok) { guilds.value = []; return }
      guilds.value = await res.json()
    } catch (e) {
      console.error('Failed to load guilds', e)
    }
  }

  async function renameGuild(guildId: string, name: string) {
    const res = await fetch(`${API_BASE}/guilds/${guildId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', ..._authHeaders() },
      body: JSON.stringify({ name })
    })
    if (!res.ok) throw new Error('Failed to rename guild')
    if (currentGuild.value && currentGuild.value.id === guildId) {
      currentGuild.value = { ...currentGuild.value, name }
    }
    const idx = guilds.value.findIndex(g => g.id === guildId)
    if (idx !== -1) guilds.value[idx] = { ...guilds.value[idx], name }
    return await res.json()
  }

  async function createGuild(name?: string) {
    const res = await fetch(`${API_BASE}/guilds`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ..._authHeaders() },
      body: JSON.stringify({ name })
    })
    const guild: Guild = await res.json()
    guilds.value.unshift(guild)
    return guild
  }

  async function joinGuild(guildId: string): Promise<Guild | null> {
    try {
      const res = await fetch(`${API_BASE}/guilds/${guildId}`)
      if (!res.ok) throw new Error('Guild not found')
      const guild: Guild = await res.json()
      currentGuild.value = guild
      messages.value = guild.messages || []
      return guild
    } catch (e) {
      console.error('Failed to join guild', e)
      return null
    }
  }

  function connectWebSocket(guildId: string, onMessage?: MessageHandler): WebSocket {
    manualClose = false
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    if (ws) {
      try { ws.close() } catch (e) { /* ignore */ }
    }

    let socket: WebSocket
    try {
      socket = new WebSocket(`ws://localhost:8000/ws/${guildId}`)
    } catch (e) {
      console.error('Failed to construct WebSocket', e)
      _scheduleReconnect(guildId, onMessage)
      // Return a dummy that callers won't use; the real one will be wired up on retry.
      return ws as WebSocket
    }
    ws = socket

    socket.onopen = () => {
      isConnected.value = true
      reconnectAttempt.value = 0
    }
    socket.onmessage = (event) => {
      let data: WSMessage
      try {
        data = JSON.parse(event.data)
      } catch (e) {
        console.warn('Dropping non-JSON WS frame', e)
        return
      }
      try {
        if (data.type === 'chat') {
          messages.value.push(data as ChatMessage)
        }
        if (data.type === 'guild-updated') {
          if (currentGuild.value && currentGuild.value.id === data.id) {
            currentGuild.value = { ...currentGuild.value, name: data.name }
          }
          const idx = guilds.value.findIndex(g => g.id === data.id)
          if (idx !== -1) guilds.value[idx] = { ...guilds.value[idx], name: data.name }
        }
        if (onMessage) onMessage(data)
        messageHandlers.value.forEach(h => {
          try { h(data) } catch (err) { console.error('WS message handler error', err) }
        })
      } catch (e) {
        console.error('Error processing WS message', e)
      }
    }
    socket.onclose = (event) => {
      isConnected.value = false
      if (manualClose) return
      if (!event.wasClean) {
        console.warn(`WebSocket closed (code=${event.code} reason=${event.reason || 'n/a'})`)
      }
      _scheduleReconnect(guildId, onMessage)
    }
    socket.onerror = (event) => {
      isConnected.value = false
      console.error('WebSocket error', event)
    }
    return socket
  }

  function _scheduleReconnect(guildId: string, onMessage?: MessageHandler) {
    if (manualClose) return
    if (reconnectAttempt.value >= MAX_RETRIES) {
      console.error(`WebSocket reconnect gave up after ${MAX_RETRIES} attempts`)
      return
    }
    const delay = _backoffDelay(reconnectAttempt.value)
    reconnectAttempt.value += 1
    console.info(`WebSocket reconnect attempt ${reconnectAttempt.value}/${MAX_RETRIES} in ${delay}ms`)
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      connectWebSocket(guildId, onMessage)
    }, delay)
  }

  function disconnectWebSocket() {
    manualClose = true
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    if (ws) {
      try { ws.close() } catch (e) { /* ignore */ }
      ws = null
    }
    isConnected.value = false
    reconnectAttempt.value = 0
  }

  function sendMessage(data: WSMessage): boolean {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      console.warn('sendMessage: socket not open, dropping', data.type)
      return false
    }
    try {
      ws.send(JSON.stringify(data))
      return true
    } catch (e) {
      console.error('sendMessage failed', e)
      return false
    }
  }

  function addMessageHandler(handler: MessageHandler) {
    messageHandlers.value.push(handler)
  }

  function removeMessageHandler(handler: MessageHandler) {
    messageHandlers.value = messageHandlers.value.filter(h => h !== handler)
  }

  async function resetForemanConversation(): Promise<void> {
    if (!currentGuild.value) return
    messages.value = []
    await fetch(`${API_BASE}/guilds/${currentGuild.value.id}/foreman/reset`, {
      method: 'POST',
      headers: _authHeaders(),
    })
  }

  async function fetchForemanHealth(): Promise<{ healthy: boolean; issues: string[]; history_length: number } | null> {
    if (!currentGuild.value) return null
    try {
      const res = await fetch(`${API_BASE}/guilds/${currentGuild.value.id}/foreman/health`, {
        headers: _authHeaders(),
      })
      if (!res.ok) return null
      return await res.json()
    } catch {
      return null
    }
  }

  return {
    currentGuild,
    guilds,
    isConnected,
    reconnectAttempt,
    messages,
    loadGuilds,
    createGuild,
    renameGuild,
    joinGuild,
    connectWebSocket,
    disconnectWebSocket,
    sendMessage,
    addMessageHandler,
    removeMessageHandler,
    resetForemanConversation,
    fetchForemanHealth,
  }
})
