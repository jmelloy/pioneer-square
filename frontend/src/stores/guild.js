import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useAuthStore } from './auth.js'

const API_BASE = 'http://localhost:8000'

export const useGuildStore = defineStore('guild', () => {
  const currentGuild = ref(null)
  const guilds = ref([])
  const isConnected = ref(false)
  const messages = ref([])
  let ws = null
  const messageHandlers = ref([])

  function _authHeaders() {
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

  async function renameGuild(guildId, name) {
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

  async function createGuild(name) {
    const res = await fetch(`${API_BASE}/guilds`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ..._authHeaders() },
      body: JSON.stringify({ name })
    })
    const guild = await res.json()
    guilds.value.unshift(guild)
    return guild
  }

  async function joinGuild(guildId) {
    try {
      const res = await fetch(`${API_BASE}/guilds/${guildId}`)
      if (!res.ok) throw new Error('Guild not found')
      const guild = await res.json()
      currentGuild.value = guild
      messages.value = guild.messages || []
      return guild
    } catch (e) {
      console.error('Failed to join guild', e)
      return null
    }
  }

  function connectWebSocket(guildId, onMessage, retryCount = 0) {
    const MAX_RETRIES = 10
    if (ws) {
      ws.close()
    }
    ws = new WebSocket(`ws://localhost:8000/ws/${guildId}`)
    ws.onopen = () => {
      isConnected.value = true
    }
    ws.onmessage = (event) => {
      const data = JSON.parse(event.data)
      if (data.type === 'chat') {
        messages.value.push(data)
      }
      if (data.type === 'guild-updated') {
        if (currentGuild.value && currentGuild.value.id === data.id) {
          currentGuild.value = { ...currentGuild.value, name: data.name }
        }
        const idx = guilds.value.findIndex(g => g.id === data.id)
        if (idx !== -1) guilds.value[idx] = { ...guilds.value[idx], name: data.name }
      }
      if (onMessage) onMessage(data)
      messageHandlers.value.forEach(h => h(data))
    }
    ws.onclose = (event) => {
      isConnected.value = false
      if (!event.wasClean && retryCount < MAX_RETRIES) {
        setTimeout(() => connectWebSocket(guildId, onMessage, retryCount + 1), 2000)
      }
    }
    ws.onerror = () => {
      isConnected.value = false
    }
    return ws
  }

  function disconnectWebSocket() {
    if (ws) {
      ws.close()
      ws = null
    }
    isConnected.value = false
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
    currentGuild,
    guilds,
    isConnected,
    messages,
    loadGuilds,
    createGuild,
    renameGuild,
    joinGuild,
    connectWebSocket,
    disconnectWebSocket,
    sendMessage,
    addMessageHandler,
    removeMessageHandler
  }
})
