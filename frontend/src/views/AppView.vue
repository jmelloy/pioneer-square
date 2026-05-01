<template>
  <div class="app-layout" :data-tab="activeTab">
    <TopBar :debug-active="showDebug" @toggle-debug="showDebug = !showDebug" />
    <div class="app-body">
      <GuildSidebar />
      <MainView />
      <ChatPane />
    </div>
    <teleport to="body">
      <DebugSidebar v-if="showDebug" @close="showDebug = false" />
    </teleport>
    <nav class="mobile-tab-bar">
      <button class="tab-btn" :class="{ active: activeTab === 'tasks' }" @click="activeTab = 'tasks'">
        <span class="tab-icon">≡</span>
        <span class="tab-label">Tasks</span>
      </button>
      <button class="tab-btn" :class="{ active: activeTab === 'work' }" @click="activeTab = 'work'">
        <span class="tab-icon">◈</span>
        <span class="tab-label">Work</span>
      </button>
      <button class="tab-btn" :class="{ active: activeTab === 'chat' }" @click="activeTab = 'chat'">
        <span class="tab-icon">✉</span>
        <span class="tab-label">Chat</span>
      </button>
    </nav>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, watch, ref, provide } from 'vue'
import { useRouter } from 'vue-router'
import { useGuildStore } from '../stores/guild'
import { useAgentsStore } from '../stores/agents'
import { useAuthStore } from '../stores/auth'
import { useGitHubStore } from '../stores/github'
import { useTasksStore } from '../stores/tasks'
import GuildSidebar from '../components/GuildSidebar.vue'
import MainView from '../components/MainView.vue'
import ChatPane from '../components/ChatPane.vue'
import TopBar from '../components/TopBar.vue'
import DebugSidebar from '../components/DebugSidebar.vue'

const props = defineProps<{ guildId?: string }>()

const activeTab = ref<'tasks' | 'work' | 'chat'>('chat')
provide('switchMobileTab', (tab: 'tasks' | 'work' | 'chat') => { activeTab.value = tab })

const showDebug = ref(false)

const router = useRouter()
const guildStore = useGuildStore()
const agentsStore = useAgentsStore()
const authStore = useAuthStore()
const ghStore = useGitHubStore()
const tasksStore = useTasksStore()

function getClientId() {
  let id = localStorage.getItem('client_id')
  if (!id) {
    id = Math.random().toString(36).slice(2, 10)
    localStorage.setItem('client_id', id)
  }
  return id
}

async function initGuild(guildId: string) {
  if (!authStore.isLoggedIn) {
    router.replace('/')
    return
  }
  if (!guildId) {
    router.replace('/')
    return
  }

  agentsStore.clearAgents()
  tasksStore.clearTasks()
  const guild = await guildStore.joinGuild(guildId)
  if (!guild) {
    router.replace('/')
    return
  }
  // Load existing tasks for this guild
  await tasksStore.fetchTasks(guildId)

  if (guild.agents) {
    guild.agents
      .filter(a => a.state !== 'offline')
      .forEach(a => agentsStore.registerAgent({
        agentId: a.id,
        agentName: a.name,
        agentType: a.type,
        workerId: a.worker_id || null,
        state: a.state,
        joinedAt: a.joined_at,
      }))
  }

  const clientId = getClientId()
  const suffix = clientId.slice(0, 4)
  const foremanName = authStore.user ? `${authStore.user.login}-${suffix}` : `Foreman-${suffix}`

  guildStore.connectWebSocket(guildId, (data) => {
    agentsStore.handleWebSocketMessage(data)
    tasksStore.handleWebSocketMessage(data)
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
  tasksStore.clearTasks()
  document.title = 'Pioneer Square'
})

watch(() => props.guildId, async (newId) => {
  if (newId) {
    await initGuild(newId)
  }
}, { immediate: true })

watch(
  () => guildStore.currentGuild?.name,
  (name) => {
    document.title = name ? `${name} — Pioneer Square` : 'Pioneer Square'
  },
  { immediate: true }
)
</script>

<style>
.app-layout {
  display: flex;
  flex-direction: column;
  height: 100vh;
  width: 100vw;
  overflow: hidden;
  background: var(--color-bg);
}

.app-body {
  flex: 1;
  display: flex;
  min-height: 0;
  overflow: hidden;
}

.mobile-tab-bar {
  display: none;
}

@media (max-width: 1024px) {
  .mobile-tab-bar {
    display: flex;
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    height: 52px;
    background: var(--color-bg-secondary);
    border-top: 2px solid var(--color-brass-dark);
    z-index: 200;
  }

  .tab-btn {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 3px;
    background: none;
    border: none;
    border-right: 1px solid var(--color-brass-dark);
    cursor: pointer;
    color: var(--color-text-dim);
    transition: color 0.12s, background 0.12s;
    padding: 4px 0;
  }

  .tab-btn:last-child {
    border-right: none;
  }

  .tab-btn.active {
    color: var(--color-brass);
    background: rgba(232, 170, 0, 0.1);
  }

  .tab-icon {
    font-size: 18px;
    line-height: 1;
  }

  .tab-label {
    font-family: var(--font-pixel);
    font-size: 6px;
    letter-spacing: 1px;
    text-transform: uppercase;
  }

  /* Each pane takes the full screen (minus top bar + bottom tab bar) and is hidden unless active */
  .sidebar,
  .main-view,
  .chat-pane {
    position: fixed !important;
    top: 44px;
    left: 0;
    right: 0;
    bottom: 52px;
    width: 100vw !important;
    min-width: 0 !important;
    max-width: 100vw !important;
    height: calc(100dvh - 96px) !important;
    max-height: calc(100dvh - 96px) !important;
    display: none !important;
  }

  .app-layout[data-tab="tasks"] .sidebar { display: flex !important; }
  .app-layout[data-tab="work"]  .main-view { display: flex !important; }
  .app-layout[data-tab="chat"]  .chat-pane { display: flex !important; }
}
</style>
