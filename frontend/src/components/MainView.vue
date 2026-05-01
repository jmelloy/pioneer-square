<template>
  <div class="main-view">
    <div class="tab-bar">
      <button
        class="tab"
        :class="{ active: activeTab === 'factory' }"
        @click="activeTab = 'factory'"
      >
        <span class="tab-icon">⚙</span>
        <span class="tab-label">Factory Floor</span>
      </button>
      <!-- Agent tabs — opened from sidebar -->
      <button
        v-for="agent in visibleAgentTabs"
        :key="'agent-' + agent.id"
        class="tab worker-tab"
        :class="{ active: activeTab === 'agent-' + agent.id }"
        @click="onAgentTabClick($event, agent.id)"
      >
        <span class="state-dot" :class="agent.state"></span>
        <span class="tab-label">{{ agent.name }}</span>
        <span class="tab-close">×</span>
      </button>
      <!-- Worker tabs — opened from sidebar worker click -->
      <button
        v-for="worker in visibleWorkerTabs"
        :key="'worker-' + worker.id"
        class="tab worker-tab"
        :class="{ active: activeTab === 'worker-' + worker.id }"
        @click="onWorkerTabClick($event, worker.id)"
      >
        <span class="state-dot" :class="worker.state"></span>
        <span class="tab-label">{{ worker.id }}</span>
        <span class="tab-close">×</span>
      </button>
      <!-- Task tabs — only shown when explicitly opened -->
      <button
        v-for="task in visibleTaskTabs"
        :key="'task-' + task.id"
        class="tab task-tab"
        :class="{ active: activeTab === 'task-' + task.id }"
        @click="onTabClick($event, task.id)"
      >
        <span class="task-dot" :class="'task-dot-' + task.state.replace(/[^a-z]/g, '-')"></span>
        <span class="tab-label">{{ task.name || task.id }}</span>
        <span class="tab-close">×</span>
      </button>
    </div>
    <div class="tab-content">
      <FactoryFloor v-if="activeTab === 'factory'" />
      <TerminalPane v-else-if="activeTab.startsWith('agent-')" :agent-id="activeTab.slice(6)" />
      <WorkerTerminalPane
        v-else-if="activeTab.startsWith('worker-')"
        :worker-id="activeTab.slice(7)"
      />
      <TaskPane v-else-if="activeTab.startsWith('task-')" :task-id="activeTab.slice(5)" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useAgentsStore } from '../stores/agents'
import { useTasksStore } from '../stores/tasks'
import FactoryFloor from './FactoryFloor.vue'
import TerminalPane from './TerminalPane.vue'
import WorkerTerminalPane from './WorkerTerminalPane.vue'
import TaskPane from './TaskPane.vue'

const agentsStore = useAgentsStore()
const tasksStore = useTasksStore()
const activeTab = ref('factory')

const visibleAgentTabs = computed(() =>
  agentsStore.openedAgentIds
    .map((id) => agentsStore.agents.find((a) => a.id === id))
    .filter(Boolean),
)

const visibleWorkerTabs = computed(() =>
  agentsStore.openedWorkerIds
    .map((id) => agentsStore.workers.find((w) => w.id === id))
    .filter(Boolean),
)

const visibleTaskTabs = computed(() =>
  tasksStore.openedTaskIds.map((id) => tasksStore.tasks.find((t) => t.id === id)).filter(Boolean),
)

watch(
  () => agentsStore.selectedAgentId,
  (id) => {
    if (id) activeTab.value = 'agent-' + id
  },
)

watch(
  () => agentsStore.selectedWorkerId,
  (id) => {
    if (id) activeTab.value = 'worker-' + id
  },
)

watch(
  () => tasksStore.selectedTaskId,
  (id) => {
    if (id) activeTab.value = 'task-' + id
  },
)

function onWorkerTabClick(event: MouseEvent, workerId: string) {
  if ((event.target as HTMLElement).closest('.tab-close')) {
    agentsStore.closeWorker(workerId)
    if (activeTab.value === 'worker-' + workerId) activeTab.value = 'factory'
  } else {
    activeTab.value = 'worker-' + workerId
  }
}

function onAgentTabClick(event: MouseEvent, agentId: string) {
  if ((event.target as HTMLElement).closest('.tab-close')) {
    agentsStore.closeAgent(agentId)
    if (activeTab.value === 'agent-' + agentId) activeTab.value = 'factory'
  } else {
    activeTab.value = 'agent-' + agentId
  }
}

function onTabClick(event: MouseEvent, taskId: string) {
  if ((event.target as HTMLElement).closest('.tab-close')) {
    tasksStore.closeTask(taskId)
    if (activeTab.value === 'task-' + taskId) activeTab.value = 'factory'
  } else {
    activeTab.value = 'task-' + taskId
  }
}
</script>

<style scoped>
.main-view {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--color-bg);
}

.tab-bar {
  display: flex;
  background: var(--color-bg-secondary);
  border-bottom: 2px solid var(--color-brass-dark);
  overflow-x: auto;
  flex-shrink: 0;
}

.tab {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 16px;
  background: none;
  border: none;
  border-right: 1px solid var(--color-bg-tertiary);
  color: var(--color-text-dim);
  cursor: pointer;
  font-family: var(--font-mono);
  font-size: 12px;
  white-space: nowrap;
  transition: all 0.15s;
}

.tab:hover {
  background: rgba(232, 170, 0, 0.07);
  color: var(--color-text);
}

.tab.active {
  background: var(--color-bg);
  color: var(--color-brass-light);
  border-bottom: 2px solid var(--color-brass);
  margin-bottom: -2px;
  box-shadow: inset 0 -2px 6px rgba(232, 170, 0, 0.1);
}

.tab-icon {
  font-size: 14px;
}
.tab-label {
  font-size: 11px;
}

.state-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
  flex-shrink: 0;
}
.state-dot.idle {
  background: var(--color-text-dim);
}
.state-dot.thinking {
  background: var(--color-blue);
  animation: dotPulse 1s infinite;
}
.state-dot.working {
  background: var(--color-green);
  animation: dotPulse 0.5s infinite;
}
.state-dot.busy {
  background: var(--color-orange);
  animation: dotPulse 0.8s infinite;
}
.state-dot.error {
  background: var(--color-red);
}

.worker-tab {
  border-left: 1px solid rgba(0, 187, 170, 0.2);
}
.worker-tab.active {
  border-bottom-color: var(--color-teal);
  color: var(--color-teal);
}

.task-tab {
  border-left: 1px solid rgba(255, 204, 0, 0.2);
}

.tab-close {
  font-size: 13px;
  line-height: 1;
  color: var(--color-text-dim);
  margin-left: 2px;
  padding: 0 2px;
  border-radius: 2px;
  opacity: 0;
  transition:
    opacity 0.1s,
    color 0.1s;
}

.worker-tab:hover .tab-close,
.worker-tab.active .tab-close,
.task-tab:hover .tab-close,
.task-tab.active .tab-close {
  opacity: 1;
}

.tab-close:hover {
  color: var(--color-red);
  background: rgba(255, 80, 80, 0.12);
}

.task-dot {
  width: 8px;
  height: 8px;
  border-radius: 2px;
  display: inline-block;
  flex-shrink: 0;
}
.task-dot-pending {
  background: var(--color-text-dim);
}
.task-dot-working {
  background: var(--color-green);
  animation: dotPulse 0.5s infinite;
}
.task-dot-awaiting-review {
  background: var(--color-amber);
  animation: dotPulse 1.5s infinite;
}
.task-dot-done {
  background: var(--color-teal);
}
.task-dot-failed {
  background: var(--color-red);
}
.task-dot-follow-up {
  background: var(--color-orange);
  animation: dotPulse 0.8s infinite;
}

@keyframes dotPulse {
  0%,
  100% {
    opacity: 1;
    transform: scale(1);
  }
  50% {
    opacity: 0.5;
    transform: scale(0.7);
  }
}

.tab-content {
  flex: 1;
  overflow: hidden;
  position: relative;
}
</style>
