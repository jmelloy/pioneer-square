<template>
  <div
    class="workers-section"
    :class="{ 'workers-section--empty': onlineWorkers.length === 0 && !showSpawnForm }"
  >
    <div class="section-header">
      <span class="section-label">Workers</span>
      <span class="section-count" v-if="onlineWorkers.length > 0">{{ onlineWorkers.length }}</span>
      <button
        class="spawn-btn"
        @click="showSpawnForm = !showSpawnForm"
        :title="showSpawnForm ? 'Cancel' : 'Launch a new worker container'"
      >
        {{ showSpawnForm ? '✕' : '+' }}
      </button>
    </div>

    <SpawnWorkerForm v-if="showSpawnForm" @launched="showSpawnForm = false" />

    <template v-for="worker in onlineWorkers" :key="worker.id">
      <div class="worker-row" :class="worker.state" @click="agentsStore.selectWorker(worker.id)">
        <span class="worker-dot" :class="'wdot-' + worker.state"></span>
        <span class="worker-row-name">{{ worker.name }}</span>
        <span class="worker-row-state">{{ worker.state }}</span>
      </div>
      <div
        v-for="agent in agentsForWorker(worker.id)"
        :key="agent.id"
        class="agent-row"
        :class="{ selected: agentsStore.selectedAgentId === agent.id }"
        title="Open agent tab"
        @click.stop="agentsStore.selectAgent(agent.id)"
      >
        <span class="agent-dot" :class="'wdot-' + agent.state"></span>
        <span class="agent-row-name">{{ agent.name }}</span>
        <div class="agent-actions">
          <button
            class="agent-icon-btn"
            :disabled="!currentTaskForWorker(worker.id)"
            :title="currentTaskForWorker(worker.id) ? 'Open current task' : 'No active task'"
            @click.stop="openAgentTask(worker.id)"
          >
            📋
          </button>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useAgentsStore } from '../../stores/agents'
import { useTasksStore } from '../../stores/tasks'
import SpawnWorkerForm from './SpawnWorkerForm.vue'

const agentsStore = useAgentsStore()
const tasksStore = useTasksStore()

const showSpawnForm = ref(false)

const onlineWorkers = computed(() => agentsStore.workers.filter((w) => w.state !== 'offline'))

function agentsForWorker(workerId: string) {
  return agentsStore.agents.filter((a) => a.workerId === workerId)
}

function currentTaskForWorker(workerId: string) {
  const active = tasksStore.tasks.filter(
    (t) => t.worker_id === workerId && !['done', 'failed'].includes(t.state),
  )
  if (active.length) return active[0]
  return tasksStore.tasks.find((t) => t.worker_id === workerId) || null
}

function openAgentTask(workerId: string) {
  const task = currentTaskForWorker(workerId)
  if (task) tasksStore.selectTask(task.id)
}
</script>

<style scoped>
.workers-section {
  border-top: 2px solid var(--color-brass-dark);
  flex-shrink: 0;
  max-height: 300px;
  overflow-y: auto;
}

.workers-section--empty {
  max-height: none;
}

.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 5px 12px 4px;
  background: var(--color-bg-secondary);
  border-bottom: 1px solid var(--color-brass-dark);
  position: sticky;
  top: 0;
}

.section-label {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-teal);
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

.spawn-btn {
  background: none;
  border: 1px solid var(--color-teal);
  color: var(--color-teal);
  cursor: pointer;
  font-size: 13px;
  line-height: 1;
  width: 18px;
  height: 18px;
  border-radius: 2px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  opacity: 0.7;
  transition:
    opacity 0.12s,
    background 0.12s;
}

.spawn-btn:hover {
  opacity: 1;
  background: rgba(0, 187, 170, 0.12);
}

.worker-row {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 5px 12px;
  background: var(--color-bg-secondary);
  border-bottom: 1px solid rgba(255, 255, 255, 0.04);
  cursor: pointer;
  transition: background 0.12s;
}

.worker-row:hover {
  background: rgba(0, 187, 170, 0.08);
}

.worker-row-name {
  font-size: 10px;
  color: var(--color-teal);
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-family: var(--font-pixel);
  letter-spacing: 0.5px;
}

.worker-row-state {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-text-dim);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.agent-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 12px 5px 22px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.02);
  transition: background 0.12s;
  cursor: pointer;
}

.agent-row:hover {
  background: rgba(0, 187, 170, 0.06);
}

.agent-row.selected {
  background: rgba(0, 187, 170, 0.12);
  border-left: 3px solid var(--color-teal);
  padding-left: 19px;
}

.agent-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  flex-shrink: 0;
}

.agent-row-name {
  font-size: 10px;
  color: var(--color-text);
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.agent-actions {
  display: flex;
  gap: 2px;
  flex-shrink: 0;
}

.agent-icon-btn {
  background: none;
  border: none;
  cursor: pointer;
  font-size: 12px;
  padding: 1px 3px;
  border-radius: 2px;
  opacity: 0.6;
  transition:
    opacity 0.12s,
    background 0.12s;
  line-height: 1;
}

.agent-icon-btn:hover:not(:disabled) {
  opacity: 1;
  background: rgba(255, 255, 255, 0.08);
}

.agent-icon-btn:disabled {
  opacity: 0.2;
  cursor: not-allowed;
}

.worker-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}
.wdot-idle {
  background: var(--color-text-dim);
}
.wdot-working {
  background: var(--color-green);
  animation: pulse 0.5s infinite;
}
.wdot-thinking {
  background: var(--color-blue);
  animation: pulse 1s infinite;
}
.wdot-busy {
  background: var(--color-orange);
  animation: pulse 0.8s infinite;
}
.wdot-error {
  background: var(--color-red);
}
.wdot-offline {
  background: #333;
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
