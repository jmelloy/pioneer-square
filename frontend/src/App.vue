<template>
  <div class="app-layout">
    <SessionSidebar />
    <MainView />
    <ChatPane />
  </div>
</template>

<script setup>
import { onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useSessionStore } from './stores/session.js'
import { useAgentsStore } from './stores/agents.js'
import SessionSidebar from './components/SessionSidebar.vue'
import MainView from './components/MainView.vue'
import ChatPane from './components/ChatPane.vue'

const route = useRoute()
const router = useRouter()
const sessionStore = useSessionStore()
const agentsStore = useAgentsStore()

async function initSession(sessionId) {
  if (!sessionId || sessionId === 'new') {
    const session = await sessionStore.createSession()
    router.replace(`/${session.id}`)
    return
  }
  await sessionStore.joinSession(sessionId)
  sessionStore.connectWebSocket(sessionId, (data) => {
    agentsStore.handleWebSocketMessage(data)
  })
}

onMounted(async () => {
  await sessionStore.loadSessions()
  const sessionId = route.params.sessionId
  await initSession(sessionId)
})

watch(() => route.params.sessionId, async (newId) => {
  if (newId && newId !== 'new') {
    await sessionStore.joinSession(newId)
    sessionStore.connectWebSocket(newId, (data) => {
      agentsStore.handleWebSocketMessage(data)
    })
  }
})
</script>

<style>
.app-layout {
  display: flex;
  height: 100vh;
  width: 100vw;
  overflow: hidden;
  background: var(--color-bg);
}
</style>
