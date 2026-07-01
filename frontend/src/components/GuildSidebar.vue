<template>
  <aside class="sidebar panel-bg">
    <div class="tab-bar">
      <button
        class="tab-btn"
        :class="{ active: activeTab === 'tasks' }"
        @click="activeTab = 'tasks'"
      >
        <span class="tab-label">Tasks</span>
      </button>
      <button
        class="tab-btn"
        :class="{ active: activeTab === 'workers' }"
        @click="activeTab = 'workers'"
      >
        <span class="tab-label">Workers</span>
        <span v-if="workerCount" class="tab-count">{{ workerCount }}</span>
      </button>
      <button
        v-if="ghStore.isConfigured"
        class="tab-btn"
        :class="{ active: activeTab === 'issues' }"
        @click="activeTab = 'issues'"
      >
        <span class="tab-label">Issues</span>
        <span v-if="ghStore.issues.length" class="tab-count">{{ ghStore.issues.length }}</span>
      </button>
    </div>

    <div class="tab-content">
      <TaskTree v-if="activeTab === 'tasks'" @open-task="onOpenTask" />
      <WorkerList v-if="activeTab === 'workers'" />
      <IssuesTab
        v-if="activeTab === 'issues' && ghStore.isConfigured"
        @select-issue="onSelectIssue"
      />
    </div>

    <div class="sidebar-footer">
      <div class="connection-status" :class="{ connected: isConnected }">
        <span class="status-dot"></span>
        {{ isConnected ? 'Connected' : 'Disconnected' }}
      </div>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, inject, onMounted, ref } from 'vue'
import { useGuildStore } from '../stores/guild'
import { useGitHubStore } from '../stores/github'
import { useTasksStore } from '../stores/tasks'
import { useAgentsStore } from '../stores/agents'
import TaskTree from './sidebar/TaskTree.vue'
import WorkerList from './sidebar/WorkerList.vue'
import IssuesTab from './chat-pane/IssuesTab.vue'
import type { GitHubIssue } from '../types'

const guildStore = useGuildStore()
const ghStore = useGitHubStore()
const tasksStore = useTasksStore()
const agentsStore = useAgentsStore()

const switchMobileTab = inject<(tab: string) => void>('switchMobileTab', () => {})
const selectIssue = inject<(issue: GitHubIssue) => void>('selectIssue', () => {})

const isConnected = computed(() => guildStore.isConnected)
const workerCount = computed(() => agentsStore.workers.filter((w) => w.state !== 'offline').length)
const activeTab = ref<'tasks' | 'workers' | 'issues'>('tasks')

onMounted(async () => {
  if (ghStore.repos.length === 0 && ghStore.token) {
    await ghStore.fetchRepos()
  }
})

function onOpenTask(taskId: string) {
  tasksStore.selectTask(taskId)
  switchMobileTab('work')
}

function onSelectIssue(issue: GitHubIssue) {
  switchMobileTab('chat')
  selectIssue(issue)
}
</script>

<style scoped>
.sidebar {
  width: 360px;
  min-width: 360px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.tab-bar {
  display: flex;
  border-bottom: 2px solid var(--color-brass-dark);
  background: var(--color-bg-tertiary);
  flex-shrink: 0;
}

.tab-btn {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  padding: 8px 4px 7px;
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  margin-bottom: -2px;
  cursor: pointer;
  color: var(--color-text-dim);
  transition:
    color 0.12s,
    background 0.12s;
}

@media (max-width: 1024px) {
  .tab-btn {
    min-height: 44px;
    padding: 12px 4px 11px;
  }
}

.tab-btn:hover {
  color: var(--color-brass);
  background: rgba(232, 170, 0, 0.06);
}

.tab-btn.active {
  color: var(--color-brass-light);
  border-bottom-color: var(--color-brass);
  background: rgba(232, 170, 0, 0.08);
}

.tab-label {
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 1.5px;
  text-transform: uppercase;
}

.tab-count {
  font-size: 9px;
  color: var(--color-text-dim);
  background: var(--color-bg);
  padding: 1px 4px;
  border-radius: 2px;
}

.tab-content {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* WorkerList: remove its top border and let it fill the tab */
.tab-content :deep(.workers-section) {
  border-top: none;
  flex: 1;
  max-height: none;
  overflow-y: auto;
}

/* IssuesTab list: fill remaining space, no fixed height constraints */
.tab-content :deep(.issues-list) {
  min-height: 0;
  max-height: none;
  flex: 1;
}

.sidebar-footer {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px 12px;
  border-top: 2px solid var(--color-brass-dark);
  background: var(--color-bg-tertiary);
  flex-shrink: 0;
}

.connection-status {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 10px;
  color: var(--color-red);
}

.connection-status.connected {
  color: var(--color-green);
}

.status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
  display: inline-block;
}

.connection-status.connected .status-dot {
  animation: pulse 2s infinite;
}

@keyframes pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.3;
  }
}
</style>
