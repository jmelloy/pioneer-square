<template>
  <div class="main-view">
    <div class="tab-bar">
      <button
        class="tab"
        :class="{ active: uiStore.activeTab === 'factory' }"
        @click="uiStore.activeTab = 'factory'"
      >
        <span class="tab-icon">⚙</span>
        <span class="tab-label">Factory Floor</span>
      </button>
      <!-- Agent tabs — opened from sidebar -->
      <button
        v-for="agent in visibleAgentTabs"
        :key="'agent-' + agent.id"
        class="tab worker-tab"
        :class="{ active: uiStore.activeTab === 'agent-' + agent.id }"
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
        :class="{ active: uiStore.activeTab === 'worker-' + worker.id }"
        @click="onWorkerTabClick($event, worker.id)"
      >
        <span class="state-dot" :class="worker.state"></span>
        <span class="tab-label">{{ worker.name }}</span>
        <span class="tab-close">×</span>
      </button>
      <!-- Task tabs — only shown when explicitly opened -->
      <button
        v-for="task in visibleTaskTabs"
        :key="taskTabId(task.id)"
        class="tab task-tab"
        :class="{ active: uiStore.activeTab === taskTabId(task.id) }"
        @click="onTabClick($event, task.id)"
      >
        <span class="task-dot" :class="'task-dot-' + task.state.replace(/[^a-z]/g, '-')"></span>
        <span class="tab-label">{{ task.name || task.id }}</span>
        <span class="tab-close">×</span>
      </button>
      <!-- Issue tabs -->
      <button
        v-for="key in ghStore.openedIssueKeys"
        :key="key"
        class="tab issue-tab"
        :class="{ active: uiStore.activeTab === key }"
        @click="onIssueTabClick($event, key)"
      >
        <span class="issue-dot">⊙</span>
        <span class="tab-label">{{ issueTabLabel(key) }}</span>
        <span class="tab-close">×</span>
      </button>
    </div>
    <div class="tab-content">
      <FactoryFloor v-if="uiStore.activeTab === 'factory'" />
      <LogPane
        v-else-if="uiStore.activeTab.startsWith('agent-')"
        kind="agent"
        :id="uiStore.activeTab.slice(6)"
      />
      <LogPane
        v-else-if="uiStore.activeTab.startsWith('worker-')"
        kind="worker"
        :id="uiStore.activeTab.slice(7)"
      />
      <LogPane
        v-else-if="uiStore.activeTab.startsWith('task-')"
        kind="task"
        :id="uiStore.activeTab.slice(5)"
      />
      <IssueViewer
        v-else-if="uiStore.activeTab.startsWith('issue-')"
        v-bind="parseIssueKey(uiStore.activeTab)"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, watch } from 'vue'
import { useAgentsStore } from '../stores/agents'
import { useTasksStore } from '../stores/tasks'
import { useUiStore, taskTabId } from '../stores/ui'
import { useGitHubStore } from '../stores/github'
import FactoryFloor from './FactoryFloor.vue'
import LogPane from './LogPane.vue'
import IssueViewer from './IssueViewer.vue'

const agentsStore = useAgentsStore()
const tasksStore = useTasksStore()
const uiStore = useUiStore()
const ghStore = useGitHubStore()

// Parse "issue-owner/repo/123" → { owner, repo, issueNumber }
function parseIssueKey(key: string): { owner: string; repo: string; issueNumber: number } {
  const rest = key.slice('issue-'.length) // "owner/repo/123"
  const lastSlash = rest.lastIndexOf('/')
  const issueNumber = parseInt(rest.slice(lastSlash + 1), 10)
  const repoPath = rest.slice(0, lastSlash) // "owner/repo"
  const firstSlash = repoPath.indexOf('/')
  const owner = repoPath.slice(0, firstSlash)
  const repo = repoPath.slice(firstSlash + 1)
  return { owner, repo, issueNumber }
}

function issueTabLabel(key: string): string {
  const { repo, issueNumber } = parseIssueKey(key)
  return `${repo}#${issueNumber}`
}

const visibleAgentTabs = computed(() =>
  uiStore.openedAgentIds.map((id) => agentsStore.agents.find((a) => a.id === id)).filter(Boolean),
)

const visibleWorkerTabs = computed(() =>
  uiStore.openedWorkerIds.map((id) => agentsStore.workers.find((w) => w.id === id)).filter(Boolean),
)

const visibleTaskTabs = computed(() =>
  uiStore.openedTaskIds.map((id) => tasksStore.tasks.find((t) => t.id === id)).filter(Boolean),
)

watch(
  () => uiStore.selectedTaskId,
  (id) => {
    if (id) uiStore.openTaskTab(id)
  },
)

function onWorkerTabClick(event: MouseEvent, workerId: string) {
  if ((event.target as HTMLElement).closest('.tab-close')) {
    uiStore.closeWorker(workerId)
  } else {
    uiStore.activeTab = 'worker-' + workerId
  }
}

function onAgentTabClick(event: MouseEvent, agentId: string) {
  if ((event.target as HTMLElement).closest('.tab-close')) {
    uiStore.closeAgent(agentId)
  } else {
    uiStore.activeTab = 'agent-' + agentId
  }
}

function onTabClick(event: MouseEvent, taskId: string) {
  if ((event.target as HTMLElement).closest('.tab-close')) {
    uiStore.closeTask(taskId)
  } else {
    uiStore.openTaskTab(taskId)
  }
}

function onIssueTabClick(event: MouseEvent, key: string) {
  if ((event.target as HTMLElement).closest('.tab-close')) {
    ghStore.closeIssueTab(key)
  } else {
    uiStore.activeTab = key
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

.issue-tab {
  border-left: 1px solid rgba(0, 187, 170, 0.2);
}

.issue-tab.active {
  border-bottom-color: var(--color-teal);
  color: var(--color-teal);
}

.issue-dot {
  font-size: 11px;
  color: currentColor;
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

/* Touch devices have no :hover — keep the close affordance visible so
   opened tabs can be closed on mobile. Bump tab + close tap targets to
   ~44pt while we're at it. */
@media (hover: none) {
  .tab {
    padding: 12px 14px;
    min-height: 44px;
  }
  .worker-tab .tab-close,
  .task-tab .tab-close {
    opacity: 1;
    font-size: 16px;
    padding: 6px 8px;
    margin-left: 4px;
  }
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
  display: flex;
  flex-direction: column;
}
</style>
