<template>
  <div class="workshop-floor">
    <FactoryAtmosphere />

    <!-- Header -->
    <div class="workshop-header">
      <span class="workshop-title">⬡ WORKSHOP FLOOR ⬡</span>
      <span class="workshop-count">
        {{ onlineCount }}/{{ allWorkers.length }} workers active
      </span>
    </div>

    <!-- Desk grid -->
    <div class="desk-grid-wrap">
      <div class="desk-grid" :class="`cols-${gridCols}`">
        <WorkerDesk
          v-for="worker in allWorkers"
          :key="worker.id"
          :worker="worker"
          :selected="agentsStore.selectedWorkerId === worker.id"
          @click="onDeskClick(worker)"
          @task-click="onTaskClick"
        />
      </div>

      <div v-if="allWorkers.length === 0" class="empty-floor">
        <div class="empty-floor-text">⚙ AWAITING WORKERS ⚙</div>
        <div class="empty-floor-sub">no workers registered</div>
      </div>
    </div>

    <TickerTape :messages="tickerMessages" />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useAgentsStore } from '../stores/agents'
import { useTasksStore } from '../stores/tasks'
import type { Worker } from '../types'
import WorkerDesk from './workshop-floor/WorkerDesk.vue'
import FactoryAtmosphere from './factory-floor/FactoryAtmosphere.vue'
import TickerTape from './factory-floor/TickerTape.vue'

const agentsStore = useAgentsStore()
const tasksStore = useTasksStore()

// Show all workers so every desk is always visible (offline = dark desk)
const allWorkers = computed<Worker[]>(() => agentsStore.workers)

const onlineCount = computed(
  () => allWorkers.value.filter((w) => w.state !== 'offline').length,
)

// Responsive column count based on worker count
const gridCols = computed(() => {
  const n = allWorkers.value.length
  if (n <= 2) return 2
  if (n <= 4) return 2
  if (n <= 9) return 3
  return 4
})

function onDeskClick(worker: Worker) {
  agentsStore.selectWorker(worker.id)
}

function onTaskClick(taskId: string) {
  tasksStore.selectTask(taskId)
}

const tickerMessages = computed(() => {
  const msgs: string[] = []
  allWorkers.value.forEach((w) => {
    msgs.push(`${w.name}: ${w.state.toUpperCase()}`)
  })
  tasksStore.liveTasks
    .filter((t) => !['done', 'failed', 'cancelled'].includes(t.state))
    .slice(0, 5)
    .forEach((t) => msgs.push(`TASK: ${t.name || t.id}`))
  if (msgs.length === 0)
    msgs.push('AWAITING WORKERS', 'SYSTEMS NOMINAL', 'BOILER PRESSURE: 87 PSI')
  return msgs
})
</script>

<style scoped>
.workshop-floor {
  width: 100%;
  height: 100%;
  background: linear-gradient(180deg, #0d0600 0%, #120900 50%, #1a0e00 100%);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  font-family: var(--font-pixel);
  position: relative;
}

/* Header */
.workshop-header {
  position: relative;
  z-index: 10;
  display: flex;
  align-items: center;
  gap: 20px;
  padding: 6px 18px;
  background: rgba(12, 7, 0, 0.88);
  border-bottom: 1px solid rgba(200, 140, 0, 0.22);
  box-shadow: 0 0 12px rgba(232, 170, 0, 0.18), inset 0 0 6px rgba(232, 170, 0, 0.04);
  flex-shrink: 0;
}

.workshop-title {
  font-size: 7px;
  letter-spacing: 2px;
  background: linear-gradient(
    90deg,
    var(--color-amber),
    var(--color-gold),
    var(--color-cream),
    var(--color-teal),
    var(--color-gold),
    var(--color-amber)
  );
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
  color: transparent;
  background-size: 250% auto;
  animation: goldShimmer 5s linear infinite;
}

.workshop-count {
  font-size: 7px;
  color: var(--color-teal);
  text-shadow: 0 0 6px var(--color-teal);
}

@keyframes goldShimmer {
  from { background-position: 0% center; }
  to { background-position: 250% center; }
}

/* Grid wrapper (scrollable, above the ticker tape) */
.desk-grid-wrap {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 20px 20px 48px; /* bottom pad clears the ticker tape */
  position: relative;
  z-index: 3;
  scrollbar-width: thin;
  scrollbar-color: rgba(200, 140, 0, 0.3) transparent;
}

.desk-grid-wrap::-webkit-scrollbar {
  width: 5px;
}
.desk-grid-wrap::-webkit-scrollbar-track {
  background: transparent;
}
.desk-grid-wrap::-webkit-scrollbar-thumb {
  background: rgba(200, 140, 0, 0.3);
  border-radius: 3px;
}

/* Grid */
.desk-grid {
  display: grid;
  gap: 16px;
  align-content: start;
}

/* Explicit column classes keep layout predictable */
.desk-grid.cols-2 { grid-template-columns: repeat(2, 1fr); }
.desk-grid.cols-3 { grid-template-columns: repeat(3, 1fr); }
.desk-grid.cols-4 { grid-template-columns: repeat(4, 1fr); }

/* Responsive override for narrow containers */
@media (max-width: 600px) {
  .desk-grid.cols-3,
  .desk-grid.cols-4 {
    grid-template-columns: repeat(2, 1fr);
  }
}
@media (max-width: 400px) {
  .desk-grid {
    grid-template-columns: 1fr !important;
  }
}

/* Empty state */
.empty-floor {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 80px 0;
  gap: 10px;
}
.empty-floor-text {
  font-size: 8px;
  letter-spacing: 3px;
  color: var(--color-brass-dark, #664422);
  opacity: 0.5;
}
.empty-floor-sub {
  font-size: 6px;
  letter-spacing: 1.5px;
  color: var(--color-text-dim, #555);
  opacity: 0.4;
}
</style>
