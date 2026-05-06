<template>
  <aside class="sidebar panel-bg">
    <div class="sidebar-header">
      <span class="sidebar-title">Tasks</span>
    </div>

    <TaskList @open-task="onOpenTask" />

    <WorkerList />

    <div v-if="ghStore.isConfigured" class="issues-section">
      <div class="section-header">
        <span class="section-label">Issues</span>
        <span v-if="ghStore.issues.length" class="section-count">{{ ghStore.issues.length }}</span>
        <button
          class="issues-refresh-btn"
          @click="issuesTabRef?.refreshIssues()"
          :disabled="ghStore.loading"
          title="Refresh issues"
        >
          {{ ghStore.loading ? '…' : '↻' }}
        </button>
      </div>
      <IssuesTab ref="issuesTabRef" @select-issue="onSelectIssue" />
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
import TaskList from './sidebar/TaskList.vue'
import WorkerList from './sidebar/WorkerList.vue'
import IssuesTab from './chat-pane/IssuesTab.vue'
import type { GitHubIssue } from '../types'

const guildStore = useGuildStore()
const ghStore = useGitHubStore()
const tasksStore = useTasksStore()

const switchMobileTab = inject<(tab: string) => void>('switchMobileTab', () => {})
const selectIssue = inject<(issue: GitHubIssue) => void>('selectIssue', () => {})

const isConnected = computed(() => guildStore.isConnected)
const issuesTabRef = ref<InstanceType<typeof IssuesTab> | null>(null)

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

.sidebar-header {
  padding: 12px 10px;
  border-bottom: 2px solid var(--color-brass-dark);
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--color-bg-tertiary);
  flex-shrink: 0;
}

.sidebar-title {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-brass-light);
  letter-spacing: 2px;
  text-transform: uppercase;
  text-shadow: 0 0 6px rgba(255, 214, 68, 0.4);
}

.issues-section {
  border-top: 2px solid var(--color-brass-dark);
  flex-shrink: 0;
  max-height: 260px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.section-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 12px 4px;
  background: var(--color-bg-secondary);
  border-bottom: 1px solid var(--color-brass-dark);
  flex-shrink: 0;
}

.section-label {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-brass);
  letter-spacing: 1.5px;
  text-transform: uppercase;
}

.section-count {
  font-size: 9px;
  color: var(--color-text-dim);
  background: var(--color-bg);
  padding: 1px 5px;
  border-radius: 2px;
}

.issues-refresh-btn {
  margin-left: auto;
  background: none;
  border: 1px solid var(--color-brass-dark);
  color: var(--color-brass);
  cursor: pointer;
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 2px;
  opacity: 0.7;
  transition:
    opacity 0.12s,
    background 0.12s;
}

.issues-refresh-btn:hover:not(:disabled) {
  opacity: 1;
  background: rgba(232, 170, 0, 0.1);
}

.issues-refresh-btn:disabled {
  opacity: 0.3;
  cursor: not-allowed;
}

/* Override IssuesTab internals for sidebar: hide its toolbar (we have our own header) */
.issues-section :deep(.issues-toolbar) {
  display: none;
}

.issues-section :deep(.issues-list) {
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
