<template>
  <div class="app-layout">
    <GuildSidebar />
    <MainView />
    <ChatPane />
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useGuildStore } from '../stores/guild.js'
import { useAgentsStore } from '../stores/agents.js'
import { useAuthStore } from '../stores/auth.js'
import { useGitHubStore } from '../stores/github.js'
import GuildSidebar from '../components/GuildSidebar.vue'
import MainView from '../components/MainView.vue'
import ChatPane from '../components/ChatPane.vue'

const props = defineProps({ guildId: String })

const router = useRouter()
const guildStore = useGuildStore()
const agentsStore = useAgentsStore()
const authStore = useAuthStore()
const ghStore = useGitHubStore()

function getClientId() {
  let id = localStorage.getItem('client_id')
  if (!id) {
    id = Math.random().toString(36).slice(2, 10)
    localStorage.setItem('client_id', id)
  }
  return id
}

async function initGuild(guildId) {
  if (!authStore.isLoggedIn) {
    router.replace('/')
    return
  }
  if (!guildId) {
    router.replace('/')
    return
  }

  agentsStore.clearAgents()
  const guild = await guildStore.joinGuild(guildId)
  if (!guild) {
    router.replace('/')
    return
  }

  if (guild.agents) {
    guild.agents
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
  const foremanName = authStore.user ? `${authStore.user.login}-${suffix}` : `Foreman-${suffix}`

  guildStore.connectWebSocket(guildId, (data) => {
    agentsStore.handleWebSocketMessage(data)
  })

  setTimeout(() => {
    guildStore.sendMessage({
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
  await guildStore.loadGuilds()
})

onUnmounted(() => {
  guildStore.disconnectWebSocket()
})

watch(() => props.guildId, async (newId) => {
  if (newId) {
    await initGuild(newId)
  }
}, { immediate: true })
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
