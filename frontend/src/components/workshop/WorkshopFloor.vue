<template>
  <div class="workshop-floor" ref="floorEl">
    <!-- Ambient background gears -->
    <div class="bg-gear bg-gear-1">⚙</div>
    <div class="bg-gear bg-gear-2">⚙</div>
    <div class="bg-gear bg-gear-3">⚙</div>

    <!-- Header -->
    <div class="floor-header">
      <span class="floor-title">⚙ PIONEER SQUARE — WORKSHOP FLOOR ⚙</span>
      <div class="floor-stats">
        <span class="stat" :class="{ active: activeCount > 0 }">
          {{ activeCount }} active
        </span>
        <span class="stat-sep">·</span>
        <span class="stat">{{ allWorkers.length }} workers</span>
        <span v-if="offlineCount > 0" class="stat stat-offline">
          <span class="stat-sep">·</span>{{ offlineCount }} offline
        </span>
      </div>
    </div>

    <!-- Empty state -->
    <div v-if="allWorkers.length === 0" class="empty-floor">
      <div class="empty-icon">⚙</div>
      <div class="empty-title">NO WORKERS CONNECTED</div>
      <div class="empty-sub">Launch a worker to populate the floor</div>
    </div>

    <!-- Desk grid -->
    <div v-else class="desks-grid" :class="`cols-${columnCount}`">
      <WorkshopDesk
        v-for="worker in allWorkers"
        :key="worker.id"
        :worker="worker"
        :task-name="currentTaskName(worker.id)"
        :desk-state="deskStateFor(worker)"
        :selected="agentsStore.selectedWorkerId === worker.id"
        @click="selectWorker(worker.id)"
      />
    </div>

    <!-- Floor ticker (bottom strip) — items doubled for seamless loop -->
    <div class="floor-ticker">
      <div class="ticker-track" :style="`--ticker-items: ${tickerMsgs.length}`">
        <span v-for="(msg, i) in tickerMsgs" :key="`a-${i}`" class="ticker-item">{{ msg }}</span>
        <span v-for="(msg, i) in tickerMsgs" :key="`b-${i}`" class="ticker-item">{{ msg }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted } from 'vue'
import { useAgentsStore } from '../../stores/agents'
import { useTasksStore } from '../../stores/tasks'
import type { Worker } from '../../types'
import WorkshopDesk from './WorkshopDesk.vue'

type DeskState = 'idle' | 'working' | 'reviewing' | 'followup' | 'offline' | 'error'

const agentsStore = useAgentsStore()
const tasksStore = useTasksStore()

// ── Workers ───────────────────────────────────────────────
// Show all workers: online first, then offline
const allWorkers = computed<Worker[]>(() => {
  const online = agentsStore.workers.filter((w) => w.state !== 'offline')
  const offline = agentsStore.workers.filter((w) => w.state === 'offline')
  return [...online, ...offline]
})

const activeCount = computed(
  () => allWorkers.value.filter((w) => ['working', 'thinking', 'busy'].includes(w.state)).length,
)
const offlineCount = computed(() => allWorkers.value.filter((w) => w.state === 'offline').length)

// ── Desk state mapping ────────────────────────────────────
function deskStateFor(worker: Worker): DeskState {
  if (worker.state === 'offline') return 'offline'
  if (worker.state === 'error') return 'error'

  const activeTask = tasksStore.tasks.find(
    (t) => t.worker_id === worker.id && !['done', 'failed', 'cancelled'].includes(t.state),
  )

  if (activeTask) {
    if (activeTask.state === 'followup') return 'followup'
    if (activeTask.state === 'awaiting-review') return 'reviewing'
    if (['working', 'planning', 'pending'].includes(activeTask.state)) return 'working'
  }

  if (worker.state === 'awaiting-review') return 'reviewing'
  if (['working', 'thinking', 'busy'].includes(worker.state)) return 'working'

  return 'idle'
}

function currentTaskName(workerId: string): string | undefined {
  const task = tasksStore.tasks.find(
    (t) => t.worker_id === workerId && !['done', 'failed', 'cancelled'].includes(t.state),
  )
  return task?.name || task?.description?.slice(0, 60) || undefined
}

function selectWorker(workerId: string) {
  agentsStore.selectWorker(workerId)
}

// ── Responsive columns ────────────────────────────────────
const floorEl = ref<HTMLElement | null>(null)
const floorW = ref(1024)

function updateWidth() {
  if (floorEl.value) {
    const w = floorEl.value.clientWidth
    if (w > 0) floorW.value = w
  }
}

const columnCount = computed(() => {
  const w = floorW.value
  const n = allWorkers.value.length
  if (n === 0) return 4
  if (w < 540) return Math.min(n, 2)
  if (w < 780) return Math.min(n, 3)
  if (w < 1100) return Math.min(n, 4)
  return Math.min(n, 5)
})

let ro: ResizeObserver | null = null

onMounted(() => {
  updateWidth()
  if (floorEl.value) {
    ro = new ResizeObserver(updateWidth)
    ro.observe(floorEl.value)
  }
})

onUnmounted(() => {
  ro?.disconnect()
})

// ── Ticker messages ───────────────────────────────────────
const tickerMsgs = computed(() => {
  const msgs: string[] = []
  allWorkers.value.forEach((w) => {
    const state = deskStateFor(w)
    const name = w.name.includes('/') ? w.name.slice(w.name.lastIndexOf('/') + 1) : w.name
    msgs.push(`${name.toUpperCase()}: ${state.toUpperCase()}`)
  })
  tasksStore.tasks
    .filter((t) => !['done', 'failed', 'cancelled'].includes(t.state))
    .slice(0, 6)
    .forEach((t) => msgs.push(`TASK: ${t.name || t.id}`))
  if (msgs.length === 0) {
    msgs.push('AWAITING WORKERS', 'BOILER PRESSURE: 87 PSI', 'SYSTEMS NOMINAL', 'PIONEER SQUARE ONLINE')
  }
  return msgs
})
</script>

<style scoped>
/* ─── Floor container ────────────────────────────────────── */
.workshop-floor {
  width: 100%;
  height: 100%;
  background: linear-gradient(180deg, #0d0600 0%, #120900 55%, #1a0e00 100%);
  position: relative;
  overflow-y: auto;
  overflow-x: hidden;
  display: flex;
  flex-direction: column;
  font-family: var(--font-pixel);
}

/* ─── Background gears ───────────────────────────────────── */
.bg-gear {
  position: fixed;
  color: rgba(232, 170, 0, 0.04);
  pointer-events: none;
  user-select: none;
  z-index: 0;
  animation: gearSpin 30s linear infinite;
}
.bg-gear-1 {
  font-size: 180px;
  right: -30px;
  top: 60px;
}
.bg-gear-2 {
  font-size: 100px;
  right: 120px;
  top: 20px;
  animation-direction: reverse;
  animation-duration: 20s;
}
.bg-gear-3 {
  font-size: 140px;
  left: -20px;
  bottom: 80px;
  animation-duration: 40s;
}
@keyframes gearSpin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}

/* ─── Header ─────────────────────────────────────────────── */
.floor-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 20px 8px;
  border-bottom: 1px solid rgba(232, 170, 0, 0.18);
  background: rgba(12, 7, 0, 0.75);
  position: sticky;
  top: 0;
  z-index: 10;
  flex-shrink: 0;
  backdrop-filter: blur(4px);
}

.floor-title {
  font-size: 7px;
  letter-spacing: 1.8px;
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
  background-size: 260% auto;
  animation: goldShimmer 6s linear infinite;
}

@keyframes goldShimmer {
  from {
    background-position: 0% center;
  }
  to {
    background-position: 260% center;
  }
}

.floor-stats {
  display: flex;
  align-items: center;
  gap: 6px;
}

.stat {
  font-size: 7px;
  color: var(--color-text-dim);
  letter-spacing: 0.5px;
  transition: color 0.3s;
}
.stat.active {
  color: var(--color-green);
  text-shadow: 0 0 6px rgba(51, 221, 136, 0.5);
}
.stat.stat-offline {
  display: contents;
}
.stat-sep {
  color: rgba(136, 102, 68, 0.4);
  font-size: 8px;
}

/* ─── Empty state ────────────────────────────────────────── */
.empty-floor {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  z-index: 1;
}
.empty-icon {
  font-size: 48px;
  color: rgba(232, 170, 0, 0.15);
  animation: gearSpin 8s linear infinite;
}
.empty-title {
  font-size: 9px;
  color: var(--color-brass-dark);
  letter-spacing: 2px;
}
.empty-sub {
  font-size: 7px;
  color: rgba(136, 102, 68, 0.5);
  letter-spacing: 1px;
  font-family: var(--font-mono);
}

/* ─── Desks grid ─────────────────────────────────────────── */
.desks-grid {
  flex: 1;
  display: grid;
  gap: 20px 12px;
  padding: 20px 20px 8px;
  align-content: start;
  z-index: 1;
}

/* column count variants */
.desks-grid.cols-1 {
  grid-template-columns: repeat(1, minmax(150px, 200px));
}
.desks-grid.cols-2 {
  grid-template-columns: repeat(2, minmax(150px, 200px));
}
.desks-grid.cols-3 {
  grid-template-columns: repeat(3, minmax(150px, 200px));
}
.desks-grid.cols-4 {
  grid-template-columns: repeat(4, minmax(150px, 200px));
}
.desks-grid.cols-5 {
  grid-template-columns: repeat(5, minmax(140px, 190px));
}

/* ─── Floor ticker ───────────────────────────────────────── */
.floor-ticker {
  flex-shrink: 0;
  height: 26px;
  overflow: hidden;
  border-top: 1px solid rgba(232, 170, 0, 0.15);
  background: rgba(8, 4, 0, 0.8);
  display: flex;
  align-items: center;
  z-index: 10;
}

.ticker-track {
  display: flex;
  gap: 0;
  white-space: nowrap;
  animation: tickerScroll 40s linear infinite;
}

.ticker-item {
  font-size: 6px;
  color: var(--color-brass-dark);
  letter-spacing: 1.5px;
  padding: 0 28px;
  border-right: 1px solid rgba(232, 170, 0, 0.12);
}

@keyframes tickerScroll {
  0% {
    transform: translateX(0);
  }
  100% {
    transform: translateX(-50%);
  }
}
</style>
