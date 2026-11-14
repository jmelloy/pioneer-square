<template>
  <div class="dashboard" :class="{ 'dashboard-floor': view === 'floor' }">
    <header class="focus-header">
      <span class="focus-icon" aria-hidden="true">💬</span>
      <div class="focus-text">
        <h1 class="focus-title">
          {{ focus ? focus.name || `Conversation ${focus.id}` : guildStore.currentGuild?.name }}
        </h1>
        <p class="focus-meta">
          <template v-if="focus">
            {{ guildStore.currentGuild?.name }} · {{ focusTaskCount }} tasks · Updated
            {{ formatRelative(focus.updated_at) }}
          </template>
          <template v-else>No active conversations</template>
        </p>
      </div>
      <div v-if="focus" class="focus-side">
        <span class="focus-stat" title="Participants"
          >👥 {{ focus.participants?.length ?? 0 }}</span
        >
        <span class="focus-stat">#{{ focus.id }}</span>
        <StatusBadge
          :label="conversationsStore.statusLabel(focus.status)"
          :color="conversationsStore.statusColor(focus.status)"
        />
        <button class="link-btn" @click="uiStore.openConversationTab(focus.id)">Open →</button>
      </div>
    </header>

    <PillTabs v-model="view" :tabs="tabs" />

    <div v-if="view === 'overview'" class="overview">
      <BaseCard v-if="focus" class="span-all" title="Latest" icon="💬">
        <ActivityFeed :messages="focusMessages" />
      </BaseCard>
      <BaseCard title="Tasks" icon="▶" :count="recentTasks.length">
        <template #actions>
          <button class="link-btn" @click="view = 'tasks'">View all →</button>
        </template>
        <TaskTable :tasks="recentTasks.slice(0, PREVIEW)" @select-task="uiStore.openTaskTab" />
      </BaseCard>
      <BaseCard
        title="Active Workers"
        icon="⚙"
        :count="activeAgents.length"
        :collapsed="workersCollapsed"
      >
        <template #actions>
          <ActiveWorkers v-if="workersCollapsed" compact :agents="workerAgents" />
          <button
            class="collapse-btn"
            :aria-expanded="!workersCollapsed"
            :title="workersCollapsed ? 'Expand workers' : 'Collapse workers'"
            @click="workersCollapsed = !workersCollapsed"
          >
            {{ workersCollapsed ? '▸' : '▾' }}
          </button>
          <button v-if="!workersCollapsed" class="link-btn" @click="view = 'workers'">
            View all →
          </button>
        </template>
        <ActiveWorkers
          :agents="activeAgents.slice(0, PREVIEW)"
          @select-agent="uiStore.selectAgent"
        />
      </BaseCard>
    </div>
    <BaseCard v-else-if="view === 'tasks'" title="Tasks" icon="▶" :count="recentTasks.length">
      <TaskTable :tasks="recentTasks" @select-task="uiStore.openTaskTab" />
    </BaseCard>
    <BaseCard
      v-else-if="view === 'workers'"
      title="Workers"
      icon="⚙"
      :count="workerAgents.length"
      :collapsed="workersCollapsed"
    >
      <template #actions>
        <ActiveWorkers v-if="workersCollapsed" compact :agents="workerAgents" />
        <button
          class="collapse-btn"
          :aria-expanded="!workersCollapsed"
          :title="workersCollapsed ? 'Expand workers' : 'Collapse workers'"
          @click="workersCollapsed = !workersCollapsed"
        >
          {{ workersCollapsed ? '▸' : '▾' }}
        </button>
      </template>
      <ActiveWorkers :agents="workerAgents" @select-agent="uiStore.selectAgent" />
    </BaseCard>
    <BaseCard v-else class="floor-card">
      <FactoryFloor />
    </BaseCard>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useAgentsStore } from '../../stores/agents'
import { useTasksStore } from '../../stores/tasks'
import { useConversationsStore } from '../../stores/conversations'
import { useGuildStore } from '../../stores/guild'
import { useUiStore } from '../../stores/ui'
import { formatRelative } from '../../utils/format'
import type { ChatMessage } from '../../types'
import FactoryFloor from '../FactoryFloor.vue'
import BaseCard from './BaseCard.vue'
import StatusBadge from './StatusBadge.vue'
import PillTabs, { type PillTab } from './PillTabs.vue'
import TaskTable from './TaskTable.vue'
import ActiveWorkers from './ActiveWorkers.vue'
import ActivityFeed from './ActivityFeed.vue'

const PREVIEW = 5

const agentsStore = useAgentsStore()
const tasksStore = useTasksStore()
const conversationsStore = useConversationsStore()
const guildStore = useGuildStore()
const uiStore = useUiStore()

// Worker section collapse is a sticky user preference; defaults to expanded.
const WORKERS_COLLAPSED_KEY = 'dashboard_workers_collapsed'
const workersCollapsed = ref(localStorage.getItem(WORKERS_COLLAPSED_KEY) === '1')
watch(workersCollapsed, (v) => localStorage.setItem(WORKERS_COLLAPSED_KEY, v ? '1' : '0'))

const view = ref<'overview' | 'tasks' | 'workers' | 'floor'>('overview')

// The most recently touched active conversation is the dashboard's headline.
const focus = computed(() =>
  conversationsStore.conversations
    .filter((c) => c.status === 'active')
    .reduce<(typeof conversationsStore.conversations)[number] | undefined>(
      (best, c) => (!best || c.updated_at > best.updated_at ? c : best),
      undefined,
    ),
)

// Newest first; ISO timestamps sort lexically.
const recentTasks = computed(() =>
  [...tasksStore.liveTasks].sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? '')),
)
const focusTaskCount = computed(
  () => recentTasks.value.filter((t) => t.conversation_id === focus.value?.id).length,
)

const workerAgents = computed(() => agentsStore.agents.filter((a) => a.type !== 'foreman'))
const activeAgents = computed(() =>
  workerAgents.value.filter((a) => a.state !== 'idle' && a.state !== 'offline'),
)

const tabs = computed<PillTab[]>(() => [
  { id: 'overview', label: 'Overview' },
  { id: 'tasks', label: 'Tasks', count: recentTasks.value.length },
  { id: 'workers', label: 'Workers', count: workerAgents.value.length },
  { id: 'floor', label: 'Factory Floor' },
])

// Refetch the headline conversation's tail whenever it changes or is bumped.
const focusMessages = ref<ChatMessage[]>([])
watch(
  () => [guildStore.currentGuild?.id, focus.value?.id, focus.value?.updated_at] as const,
  async ([guildId, id]) => {
    if (!guildId || id == null) {
      focusMessages.value = []
      return
    }
    const page = await conversationsStore.fetchConversationMessages(guildId, id)
    if (focus.value?.id !== id) return // a newer focus won the race
    focusMessages.value = page
      .filter((m) => m.role !== 'tool_use' && m.role !== 'tool_result' && m.content)
      .slice(-3)
  },
  { immediate: true },
)
</script>

<style scoped>
.dashboard {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 0;
  padding: 14px;
  overflow: auto;
  background: rgba(18, 9, 0, 0.92);
}
.focus-header {
  display: flex;
  align-items: center;
  gap: 14px;
  padding-bottom: 12px;
  border-bottom: 2px solid var(--color-brass-dark);
}
.focus-icon {
  flex-shrink: 0;
  width: 44px;
  height: 44px;
  display: grid;
  place-items: center;
  border: 2px solid var(--color-brass);
  border-radius: 50%;
  background: var(--color-bg-tertiary);
  font-size: 20px;
}
.focus-text {
  min-width: 0;
  flex: 1;
}
.focus-title {
  font-family: var(--font-mono);
  font-size: 22px;
  color: var(--color-brass-light);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.focus-meta {
  margin-top: 2px;
  color: var(--color-text-dim);
  font-size: 14px;
}
.focus-side {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-shrink: 0;
  font-size: 15px;
}
.collapse-btn {
  background: none;
  border: none;
  color: var(--color-brass-light);
  font-size: 14px;
  cursor: pointer;
}
.link-btn {
  background: none;
  border: none;
  color: var(--color-blue);
  font-family: var(--font-mono);
  font-size: 14px;
  text-decoration: underline;
  text-underline-offset: 3px;
  cursor: pointer;
  white-space: nowrap;
}
.link-btn:hover {
  color: var(--color-brass-light);
}
.overview {
  display: grid;
  /* Side by side only when each card gets room for its table columns. */
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 460px), 1fr));
  gap: 12px;
  align-items: start;
}
.span-all {
  grid-column: 1 / -1;
}
.dashboard-floor {
  overflow: hidden;
}
.floor-card {
  flex: 1;
}
.floor-card :deep(.card-body) {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
@media (max-width: 700px) {
  .focus-header {
    flex-wrap: wrap;
  }
  .focus-side {
    width: 100%;
    flex-wrap: wrap;
  }
}
</style>
