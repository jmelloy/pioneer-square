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
import { useSessionStore } from '../stores/session.js'
import { useAgentsStore } from '../stores/agents.js'
import { useGitHubStore } from '../stores/github.js'
import SessionSidebar from '../components/SessionSidebar.vue'
import MainView from '../components/MainView.vue'
import ChatPane from '../components/ChatPane.vue'

const props = defineProps({ sessionId: String })

const route = useRoute()
const router = useRouter()
const sessionStore = useSessionStore()
const agentsStore = useAgentsStore()
const ghStore = useGitHubStore()

function getClientId() {
  let id = localStorage.getItem('client_id')
  if (!id) {
    id = Math.random().toString(36).slice(2, 10)
    localStorage.setItem('client_id', id)
  }
  return id
}

async function initSession(sessionId) {
  if (!sessionId) {
    router.replace('/')
    return
  }

  agentsStore.clearAgents()
  const session = await sessionStore.joinSession(sessionId)
  if (!session) {
    router.replace('/')
    return
  }

  if (session.agents) {
    session.agents
      .filter(a => a.state !== 'offline')
      .forEach(a => agentsStore.registerAgent({
        agentId: a.id,
        agentName: a.name,
        agentType: a.type,
        state: a.state,
        joinedAt: a.joined_at,
      }))
  }

  const clientId = getClientId()
  const suffix = clientId.slice(0, 4)
  const foremanName = ghStore.user ? `${ghStore.user.login}-${suffix}` : `Foreman-${suffix}`

  sessionStore.connectWebSocket(sessionId, (data) => {
    agentsStore.handleWebSocketMessage(data)
  })

  setTimeout(() => {
    sessionStore.sendMessage({
      type: 'join',
      agentId: `foreman-${clientId}`,
      agentName: foremanName,
      agentType: 'foreman',
    })
  }, 200)

  if (ghStore.isConfigured && ghStore.selectedRepos.length > 0) {
    ghStore.fetchIssues()
  }
}

onMounted(async () => {
  await sessionStore.loadSessions()
  await initSession(props.sessionId)
})

watch(() => props.sessionId, async (newId) => {
  if (newId) {
    await initSession(newId)
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
