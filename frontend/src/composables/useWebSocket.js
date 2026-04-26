import { ref, onUnmounted } from 'vue'

export function useWebSocket(url, onMessage) {
  const ws = ref(null)
  const isConnected = ref(false)
  let reconnectTimer = null
  let shouldReconnect = true

  function connect() {
    if (ws.value) ws.value.close()
    ws.value = new WebSocket(url)
    ws.value.onopen = () => { isConnected.value = true }
    ws.value.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        if (onMessage) onMessage(data)
      } catch (err) {
        console.warn('WebSocket message parse error:', err)
      }
    }
    ws.value.onclose = () => {
      isConnected.value = false
      if (shouldReconnect) {
        reconnectTimer = setTimeout(connect, 2000)
      }
    }
    ws.value.onerror = () => { isConnected.value = false }
  }

  function send(data) {
    if (ws.value && ws.value.readyState === WebSocket.OPEN) {
      ws.value.send(JSON.stringify(data))
    }
  }

  function disconnect() {
    shouldReconnect = false
    if (reconnectTimer) clearTimeout(reconnectTimer)
    if (ws.value) ws.value.close()
  }

  onUnmounted(disconnect)
  connect()

  return { isConnected, send, disconnect }
}
