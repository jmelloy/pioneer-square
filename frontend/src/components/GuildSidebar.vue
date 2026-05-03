<template>
  <aside class="sidebar panel-bg">
    <div class="sidebar-header">
      <span class="sidebar-title">Tasks</span>
    </div>

    <TaskList @open-task="onOpenTask" />

    <WorkerList />

    <div class="sidebar-footer">
      <div class="connection-status" :class="{ connected: isConnected }">
        <span class="status-dot"></span>
        {{ isConnected ? 'Connected' : 'Disconnected' }}
      </div>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, inject, onMounted } from 'vue'
import { useGuildStore } from '../stores/guild'
import { useGitHubStore } from '../stores/github'
import { useTasksStore } from '../stores/tasks'
import TaskList from './sidebar/TaskList.vue'
import WorkerList from './sidebar/WorkerList.vue'

const guildStore = useGuildStore()
const ghStore = useGitHubStore()
const tasksStore = useTasksStore()

const switchMobileTab = inject<(tab: string) => void>('switchMobileTab', () => {})

const isConnected = computed(() => guildStore.isConnected)

onMounted(async () => {
  if (ghStore.repos.length === 0 && ghStore.token) {
    await ghStore.fetchRepos()
  }
})

function onOpenTask(taskId: string) {
  tasksStore.selectTask(taskId)
  switchMobileTab('work')
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
