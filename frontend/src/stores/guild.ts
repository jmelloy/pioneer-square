import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useAuthStore } from './auth'
import { useAgentsStore } from './agents'
import { api } from '../utils/api'
import type { ChatMessage, Guild, WSInbound, WSOutbound } from '../types'

type MessageHandler = (data: WSInbound) => void

export const useGuildStore = defineStore('guild', () => {
  const currentGuild = ref<Guild | null>(null)
  const guilds = ref<Guild[]>([])
  const isConnected = ref(false)
  const messages = ref<ChatMessage[]>([])
  const reconnectAttempt = ref(0)
  // Epoch ms when the next foreman poll check fires, from foreman-poll-status frames.
  const nextPollAt = ref<number | null>(null)
  let ws: WebSocket | null = null
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let manualClose = false

  const MAX_RETRIES = 10
  const BASE_DELAY_MS = 1000
  const MAX_DELAY_MS = 30000

  function _backoffDelay(attempt: number): number {
    const exp = Math.min(MAX_DELAY_MS, BASE_DELAY_MS * 2 ** attempt)
    // Full jitter: random value in [0, exp]
    return Math.floor(Math.random() * exp)
  }

  async function loadGuilds() {
    try {
      guilds.value = await api<Guild[]>('/guilds')
    } catch (e) {
      console.error('Failed to load guilds', e)
      guilds.value = []
    }
  }

  async function renameGuild(guildId: string, name: string) {
    return updateGuild(guildId, { name })
  }

  async function updateGuild(
    guildId: string,
    updates: { name?: string; primary_repo?: string | null },
  ) {
    const result = await api<Guild>(`/guilds/${guildId}`, { method: 'PATCH', json: updates })
    if (currentGuild.value && currentGuild.value.id === guildId) {
      currentGuild.value = { ...currentGuild.value, ...updates }
    }
    const idx = guilds.value.findIndex((g) => g.id === guildId)
    if (idx !== -1) guilds.value[idx] = { ...guilds.value[idx], ...updates }
    return result
  }

  async function createGuild(name?: string) {
    const guild = await api<Guild>('/guilds', { method: 'POST', json: { name } })
    guilds.value.unshift(guild)
    return guild
  }

  async function joinGuild(guildId: string): Promise<Guild | null> {
    try {
      const guild = await api<Guild>(`/guilds/${guildId}`)
      currentGuild.value = guild
      messages.value = (guild.messages || []).filter(
        (m: ChatMessage) => (m.from || m.from_agent) !== 'github',
      )
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
      try {
        ws.close()
      } catch {
        /* ignore */
      }
    }

    let socket: WebSocket
    try {
      const wsProto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      const authStore = useAuthStore()
      const tokenParam = authStore.loginToken
        ? `?token=${encodeURIComponent(authStore.loginToken)}`
        : ''
      socket = new WebSocket(`${wsProto}//${window.location.host}/ws/${guildId}${tokenParam}`)
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
      let data: WSInbound
      try {
        data = JSON.parse(event.data) as WSInbound
      } catch (e) {
        console.warn('Dropping non-JSON WS frame', e)
        return
      }
      try {
        handleWebSocketMessage(data)
        if (onMessage) onMessage(data)
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
    console.info(
      `WebSocket reconnect attempt ${reconnectAttempt.value}/${MAX_RETRIES} in ${delay}ms`,
    )
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
      try {
        ws.close()
      } catch {
        /* ignore */
      }
      ws = null
    }
    isConnected.value = false
    reconnectAttempt.value = 0
  }

  function sendMessage(data: WSOutbound): boolean {
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

  function handleWebSocketMessage(data: WSInbound) {
    if (data.type === 'chat') {
      const chatMsg = data as ChatMessage
      if ((chatMsg.from || chatMsg.from_agent) !== 'github') {
        // Replace any matching optimistic local entry instead of duplicating
        const localIdx = messages.value.findIndex(
          (m) => m._local && m.from === chatMsg.from && m.content === chatMsg.content,
        )
        if (localIdx !== -1) {
          messages.value.splice(localIdx, 1, chatMsg)
        } else {
          messages.value.push(chatMsg)
        }
      }
    } else if (data.type === 'guild-updated') {
      if (currentGuild.value && currentGuild.value.id === data.id) {
        currentGuild.value = { ...currentGuild.value, name: data.name }
      }
      const idx = guilds.value.findIndex((g) => g.id === data.id)
      if (idx !== -1) guilds.value[idx] = { ...guilds.value[idx], name: data.name }
    } else if (data.type === 'task-complete') {
      const agentsStore = useAgentsStore()
      messages.value.push({
        type: 'chat',
        from: 'system',
        to: 'user',
        content: data.prUrl
          ? `✓ ${agentsStore.workerDisplayName(data.workerId)} done — PR: ${data.prUrl}`
          : `✓ ${agentsStore.workerDisplayName(data.workerId)} finished (no PR)`,
        prUrl: data.prUrl || null,
        createdAt: new Date().toISOString(),
      })
    } else if (data.type === 'needs-input') {
      const agentsStore = useAgentsStore()
      messages.value.push({
        type: 'chat',
        from: 'system',
        to: 'user',
        content: `⚠ ${agentsStore.workerDisplayName(data.workerId)} needs attention on: "${data.description}"`,
        createdAt: new Date().toISOString(),
      })
    } else if (data.type === 'task-assigned') {
      const agentsStore = useAgentsStore()
      const taskName = (data.name || data.description || '').slice(0, 60)
      messages.value.push({
        type: 'chat',
        from: 'system',
        to: 'user',
        content: `→ ${agentsStore.workerDisplayName(data.workerId)} assigned: ${taskName}`,
        createdAt: new Date().toISOString(),
      })
    } else if (data.type === 'foreman-poll-status') {
      const secs = typeof data.nextCheckIn === 'number' ? data.nextCheckIn : null
      nextPollAt.value = secs !== null ? Date.now() + secs * 1000 : null
    }
  }

  return {
    currentGuild,
    guilds,
    isConnected,
    reconnectAttempt,
    messages,
    nextPollAt,
    loadGuilds,
    createGuild,
    renameGuild,
    updateGuild,
    joinGuild,
    connectWebSocket,
    disconnectWebSocket,
    sendMessage,
    handleWebSocketMessage,
  }
})
