import { defineStore } from 'pinia'
import { ref } from 'vue'

const API_BASE = 'http://localhost:8000'

export const useGuildStore = defineStore('guild', () => {
  const currentGuild = ref(null)
  const guilds = ref([])
  const isConnected = ref(false)
  const messages = ref([])
  let ws = null
  const messageHandlers = ref([])

  async function loadGuilds() {
    try {
      const res = await fetch(`${API_BASE}/guilds`)
      guilds.value = await res.json()
    } catch (e) {
      console.error('Failed to load guilds', e)
    }
  }

  async function createGuild(name) {
    const res = await fetch(`${API_BASE}/guilds`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
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
    joinGuild,
    connectWebSocket,
    disconnectWebSocket,
    sendMessage,
    addMessageHandler,
    removeMessageHandler
  }
})
