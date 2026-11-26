<template>
  <div
    :key="props.guildId"
    class="app-layout ui-surface"
    :data-tab="activeTab"
    :data-workspace="uiStore.workspace"
  >
    <TopBar :debug-active="showDebug" @toggle-debug="showDebug = !showDebug" />
    <div v-show="uiStore.workspace === 'work'" class="app-body">
      <GuildSidebar />
      <MainView />
      <ChatPane ref="chatPaneRef" />
    </div>
    <ProjectsView v-if="uiStore.workspace === 'projects'" :key="props.guildId" />
    <teleport to="body">
      <DebugSidebar v-if="showDebug" @close="showDebug = false" />
    </teleport>
    <nav class="mobile-tab-bar">
      <button
        class="tab-btn"
        :class="{ active: activeTab === 'tasks' }"
        @click="activeTab = 'tasks'"
      >
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
import { onMounted, onUnmounted, watch, ref, provide, toRef, type Ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQueryRef } from '../composables/useQueryRef'
import { getRootOrigin } from '../router'
import { useGuildStore } from '../stores/guild'
import { useAgentsStore } from '../stores/agents'
import { useAuthStore } from '../stores/auth'
import { useGitHubStore } from '../stores/github'
import { useTasksStore } from '../stores/tasks'
import { useConversationsStore } from '../stores/conversations'
import { useUsageStore } from '../stores/usage'
import GuildSidebar from '../components/GuildSidebar.vue'
import MainView from '../components/MainView.vue'
import ChatPane from '../components/ChatPane.vue'
import TopBar from '../components/TopBar.vue'
import DebugSidebar from '../components/DebugSidebar.vue'
import ProjectsView from '../components/ProjectsView.vue'
import { useUiStore } from '../stores/ui'
import '../workspace.css'

const props = defineProps<{ guildId?: string }>()

const activeTab = ref<'tasks' | 'work' | 'chat'>('chat')
provide('switchMobileTab', (tab: 'tasks' | 'work' | 'chat') => {
  activeTab.value = tab
})

const chatPaneRef = ref<InstanceType<typeof ChatPane> | null>(null)

const showDebug = ref(false)

const router = useRouter()
const guildStore = useGuildStore()
const agentsStore = useAgentsStore()
const authStore = useAuthStore()
const ghStore = useGitHubStore()
const tasksStore = useTasksStore()
const conversationsStore = useConversationsStore()
const usageStore = useUsageStore()
const uiStore = useUiStore()

const route = useRoute()
useQueryRef('view', toRef(uiStore, 'workspace') as Ref<string>, 'work', ['work', 'projects'])
useQueryRef('tab', toRef(uiStore, 'activeTab'), 'factory')

watch(
  () => uiStore.activeTab,
  (tab) => {
    // Back/forward already restored the workspace from the URL; don't override it.
    if ((route.query.tab || 'factory') !== tab) uiStore.workspace = 'work'
    ensureTabOpen(tab)
  },
  { immediate: true },
)

// Puts a tab restored from the URL (deep link, back/forward) into the tab bar.
function ensureTabOpen(tab: string) {
  uiStore.ensureTabOpen(tab)
  if (tab.startsWith('issue-') && !ghStore.openedIssueKeys.includes(tab)) {
    ghStore.openedIssueKeys.push(tab)
  }
}

let guildGeneration = 0

function clearGuildState() {
  guildGeneration++
  guildStore.clearGuild()
  agentsStore.clearAgents()
  tasksStore.clearTasks()
  conversationsStore.clearConversations()
  usageStore.clearUsage()
  ghStore.clearIssues()
}

async function initGuild(guildId: string) {
  if (!authStore.isLoggedIn) {
    // On a subdomain, redirect to the root domain bridge so it can pass
    // the session token back via ?login_token= query param.
    const rootOrigin = getRootOrigin()
    if (rootOrigin) {
      window.location.href = `${rootOrigin}/?to=${encodeURIComponent(window.location.origin)}`
    } else {
      router.replace('/')
    }
    return
  }
  if (!guildId) {
    router.replace('/')
    return
  }

  clearGuildState()
  const generation = guildGeneration
  // The clears above reset the main tab; keep the one a deep link asked for.
  const urlTab = route.query.tab
  if (typeof urlTab === 'string' && urlTab) {
    uiStore.activeTab = urlTab
    ensureTabOpen(urlTab)
  }
  const guild = await guildStore.joinGuild(guildId)
  if (generation !== guildGeneration) return
  if (!guild) {
    router.replace('/')
    return
  }
  // Load existing tasks and conversations for this guild
  await tasksStore.fetchTasks(guildId)
  if (generation !== guildGeneration) return
  conversationsStore.fetchConversations(guildId)
  usageStore.fetchUsage(guildId)

  if (guild.agents) {
    guild.agents
      .filter((a) => a.state !== 'offline')
      .forEach((a) =>
        agentsStore.registerAgent({
          agentId: a.id,
          agentName: a.name,
          agentType: a.type,
          workerId: a.worker_id || null,
          workerName: a.worker_name || undefined,
          state: a.state,
          taskId: a.current_task_id ?? null,
          joinedAt: a.joined_at,
        }),
      )
  }

  // Routing lives in guildStore: agents/tasks/conversations/usage each
  // declared their own interest via subscribeWS when their store was first used.
  guildStore.connectWebSocket(guildId)

  // The Projects screen loads its own wider scope; a primary-only fetch here would race it.
  if (ghStore.isConfigured && uiStore.workspace !== 'projects') {
    const reposToFetch = guild.primary_repo ? [guild.primary_repo] : undefined
    if (reposToFetch || ghStore.selectedRepos.length > 0) {
      ghStore.fetchIssues(reposToFetch)
    }
  }
}

// Restore session from OAuth callback params synchronously — must happen before
// the immediate watch below so initGuild's isLoggedIn check sees the token.
{
  const _p = new URLSearchParams(window.location.search)
  if (_p.has('login_token')) {
    authStore.restoreFromCallback(_p)
    ghStore.restoreGitHubToken(_p)
    window.history.replaceState({}, '', window.location.pathname)
  }
}

onMounted(async () => {
  await guildStore.loadGuilds()
})

onUnmounted(() => {
  clearGuildState()
  document.title = 'Pioneer Square'
})

watch(
  () => props.guildId,
  async (newId) => {
    if (newId) {
      await initGuild(newId)
    }
  },
  { immediate: true },
)

watch(
  () => [guildStore.currentGuild?.name, guildStore.currentGuild?.id],
  ([name, id]) => {
    const guild = guildStore.currentGuild
    if (!guild) {
      document.title = 'Pioneer Square'
      return
    }
    if (!name || !id) {
      console.warn('Guild is missing name or id', { name, id })
    }
    const label = name && id ? `${name} / ${id}` : name || id
    document.title = `Pioneer Square / ${label}`
  },
  { immediate: true },
)
</script>

<style>
.app-layout {
  display: flex;
  flex-direction: column;
  height: 100dvh;
  width: 100%;
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
    padding-bottom: env(safe-area-inset-bottom);
    box-sizing: content-box;
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
    transition:
      color 0.12s,
      background 0.12s;
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
    font: 600 var(--text-caption) / var(--leading-heading) var(--font-pixel);
    text-transform: uppercase;
  }

  .app-body {
    margin-bottom: calc(52px + env(safe-area-inset-bottom));
  }

  /* Let the header determine the available height, including when its navigation wraps. */
  .sidebar,
  .main-view,
  .chat-pane {
    position: relative !important;
    flex: 1;
    width: 100% !important;
    min-width: 0 !important;
    max-width: 100% !important;
    height: auto !important;
    max-height: 100% !important;
    display: none !important;
  }

  .app-layout[data-tab='tasks'] .sidebar {
    display: flex !important;
  }
  .app-layout[data-tab='work'] .main-view {
    display: flex !important;
  }
  .app-layout[data-tab='chat'] .chat-pane {
    display: flex !important;
  }
}
</style>
