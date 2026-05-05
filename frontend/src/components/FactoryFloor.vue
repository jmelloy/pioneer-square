<template>
  <div class="factory-floor" ref="floorEl" :class="[`size-${floorSize}`, `density-${rowDensity}`]">
    <FactoryAtmosphere />

    <!-- Header -->
    <div class="factory-info">
      <span class="factory-title">⚙ PIONEER SQUARE WORKSHOP ⚙</span>
      <span class="agent-count">Agents: {{ agents.length }}</span>
    </div>

    <!-- Tasks table — each row is a workbench -->
    <div
      class="task-table"
      :style="`top: ${TABLE_TOP}px; left: ${TABLE_LEFT}px; width: ${tableWidth}px;`"
    >
      <WorkbenchRow
        v-for="row in taskRows"
        :key="row.task ? row.task.id : `empty-${row.index}`"
        :task="row.task"
        :index="row.index"
        :row-height="rowHeight"
        :activity-key="row.activityKey"
        :stations="STATIONS"
        :state-label="stateLabel"
      />
    </div>

    <!-- Break room (idle agents lounge here) -->
    <div
      class="break-room"
      :style="`top: ${breakRoomTop}px; left: ${TABLE_LEFT}px; width: ${tableWidth}px;`"
    >
      <div class="break-label">⏚ BREAK ROOM ⏚</div>

      <!-- Couch — left wall -->
      <div class="br-couch">
        <div class="couch-back"></div>
        <div class="couch-seat">
          <div class="couch-arm couch-arm-l"></div>
          <div class="couch-arm couch-arm-r"></div>
        </div>
      </div>

      <!-- Three tables -->
      <div class="br-table br-table-1">
        <div class="br-table-top"></div>
        <div class="br-table-legs"><span></span><span></span></div>
      </div>
      <div class="br-table br-table-2">
        <div class="br-table-top"></div>
        <div class="br-table-legs"><span></span><span></span></div>
      </div>
      <div class="br-table br-table-3">
        <div class="br-table-top"></div>
        <div class="br-table-legs"><span></span><span></span></div>
      </div>

      <!-- Water cooler -->
      <div class="br-cooler">
        <div class="cooler-jug"></div>
        <div class="cooler-body"><div class="cooler-spigot"></div></div>
      </div>

      <!-- Bulletin board — right wall -->
      <div class="br-board">
        <div class="board-pin pin-a"></div>
        <div class="board-pin pin-b"></div>
        <div class="board-pin pin-c"></div>
      </div>
    </div>

    <!-- Agents — single overlay; positioned absolutely over the floor -->
    <div
      v-for="agent in agents"
      :key="agent.id"
      class="floating-agent"
      :style="`left: ${choreography.getPos(agent.id).x}px; top: ${
        choreography.getPos(agent.id).y
      }px; --walk-dur: ${choreography.duration[agent.id] || 1.5}s;`"
    >
      <AgentAvatar :agent="agent" :walking="!!choreography.walking[agent.id]" />
    </div>

    <TickerTape :messages="tickerMessages" />
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted } from 'vue'
import { useAgentsStore } from '../stores/agents'
import { useTasksStore } from '../stores/tasks'
import type { AgentActivity, Task } from '../types'
import AgentAvatar from './AgentAvatar.vue'
import FactoryAtmosphere from './factory-floor/FactoryAtmosphere.vue'
import WorkbenchRow from './factory-floor/WorkbenchRow.vue'
import TickerTape from './factory-floor/TickerTape.vue'
import { useAgentChoreography } from '../composables/useAgentChoreography'

const agentsStore = useAgentsStore()
const tasksStore = useTasksStore()
const agents = computed(() => agentsStore.agents.filter((a) => a.state !== 'offline'))

// ── Stations on each bench ─────────────────────────────────
const STATIONS = [
  { key: 'reading', label: 'ARCHIVE', frac: 0.1, component: 'archive' },
  { key: 'searching', label: 'SCRYING', frac: 0.25, component: 'orb' },
  { key: 'fetching', label: 'TELEGRAPH', frac: 0.4, component: 'telegraph' },
  { key: 'thinking', label: 'THINK TANK', frac: 0.56, component: 'tank' },
  { key: 'running', label: 'ENGINE', frac: 0.72, component: 'engine' },
  { key: 'editing', label: 'FORGE', frac: 0.88, component: 'forge' },
] as const

type StationKey = (typeof STATIONS)[number]['key']

// `planning` shares the archive station with `reading`.
const ACTIVITY_TO_STATION: Record<AgentActivity, StationKey> = {
  reading: 'reading',
  planning: 'reading',
  searching: 'searching',
  fetching: 'fetching',
  thinking: 'thinking',
  running: 'running',
  editing: 'editing',
}

// ── Layout geometry ────────────────────────────────────────
const CHAT_PANE_W = 360
const TABLE_LEFT = 28
const TABLE_TOP = 56
const ROW_GAP = 8
const MAX_ROWS = 4
const TICKER_H = 28
const BREAK_PAD = 20
const BREAK_ROOM_H = 68
const ROW_H_MIN = 70
const ROW_H_MAX = 124

const floorEl = ref<HTMLElement | null>(null)
const floorW = ref(900)
const floorH = ref(600)

function updateFloorSize() {
  if (floorEl.value) {
    floorW.value = floorEl.value.clientWidth || 900
    floorH.value = floorEl.value.clientHeight || 600
  }
}

const floorSize = computed<'sm' | 'md' | 'lg'>(() => {
  if (floorW.value < 720) return 'sm'
  if (floorW.value < 1100) return 'md'
  return 'lg'
})

const tableWidth = computed(() => {
  const reservedRight = floorSize.value === 'sm' ? 0 : CHAT_PANE_W
  return Math.max(floorW.value - reservedRight - TABLE_LEFT * 2, 360)
})

const visibleRowCount = computed(() => {
  const active = activeTasks.value.length
  return Math.max(2, Math.min(MAX_ROWS, active || 2))
})

const rowHeight = computed(() => {
  const reserved = TABLE_TOP + BREAK_PAD + BREAK_ROOM_H + TICKER_H + 16
  const available = Math.max(160, floorH.value - reserved)
  const n = visibleRowCount.value
  const candidate = (available - (n - 1) * ROW_GAP) / n
  return Math.max(ROW_H_MIN, Math.min(ROW_H_MAX, Math.round(candidate)))
})

const rowDensity = computed<'tall' | 'medium' | 'short'>(() => {
  const h = rowHeight.value
  if (h >= 100) return 'tall'
  if (h >= 82) return 'medium'
  return 'short'
})

const tableHeight = computed(
  () => visibleRowCount.value * rowHeight.value + (visibleRowCount.value - 1) * ROW_GAP,
)

const breakRoomTop = computed(() => TABLE_TOP + tableHeight.value + BREAK_PAD)

const breakRoomHeight = computed(() =>
  Math.max(60, floorH.value - breakRoomTop.value - TICKER_H - 8),
)

// ── Tasks → rows ───────────────────────────────────────────
const activeTasks = computed<Task[]>(() =>
  tasksStore.tasks
    .filter(
      (t) =>
        !['done', 'failed', 'cancelled'].includes(t.state) &&
        t.worker_id &&
        t.worker_id !== 'foreman',
    )
    .slice(0, MAX_ROWS),
)

const taskRows = computed(() => {
  const tasks = activeTasks.value
  const rows = []
  for (let i = 0; i < visibleRowCount.value; i++) {
    const task = tasks[i] || null
    const agent = task ? agents.value.find((a) => a.workerId === task.worker_id) || null : null
    const activityKey: StationKey | null =
      agent && agent.activity ? ACTIVITY_TO_STATION[agent.activity] : null
    rows.push({ index: i, task, agent, activityKey })
  }
  return rows
})

const choreography = useAgentChoreography({
  agents,
  taskRows: computed(() =>
    taskRows.value.map((r) => ({ index: r.index, task: r.task, activityKey: r.activityKey })),
  ),
  rowHeight,
  tableWidth,
  breakRoomTop,
  breakRoomHeight,
  tableLeft: TABLE_LEFT,
  tableTop: TABLE_TOP,
  rowGap: ROW_GAP,
  stations: STATIONS,
})

onMounted(() => {
  updateFloorSize()
  window.addEventListener('resize', updateFloorSize)
  choreography.syncAll()
})

onUnmounted(() => {
  window.removeEventListener('resize', updateFloorSize)
})

// ── Ticker ─────────────────────────────────────────────────
const tickerMessages = computed(() => {
  const msgs: string[] = []
  agents.value.forEach((a) => {
    const label = a.activity ? `${a.state.toUpperCase()} [${a.activity}]` : a.state.toUpperCase()
    msgs.push(`${a.name}: ${label}`)
  })
  tasksStore.tasks
    .filter((t) => !['done', 'failed', 'cancelled'].includes(t.state))
    .slice(0, 4)
    .forEach((t) => msgs.push(`TASK: ${t.name || t.id}`))
  if (msgs.length === 0) msgs.push('AWAITING WORKERS', 'SYSTEMS NOMINAL', 'BOILER PRESSURE: 87 PSI')
  return msgs
})

function stateLabel(state: string) {
  return tasksStore.stateLabel(state)
}
</script>

<style scoped>
.factory-floor {
  width: 100%;
  height: 100%;
  background: linear-gradient(180deg, #0d0600 0%, #120900 50%, #1a0e00 100%);
  position: relative;
  overflow: hidden;
  font-family: var(--font-pixel);
}

/* Header overlay */
.factory-info {
  position: absolute;
  top: 8px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  gap: 20px;
  align-items: center;
  background: rgba(12, 7, 0, 0.88);
  border: 1px solid var(--color-brass);
  padding: 4px 14px;
  z-index: 10;
  box-shadow:
    0 0 12px rgba(232, 170, 0, 0.3),
    inset 0 0 6px rgba(232, 170, 0, 0.06);
}

.factory-title {
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

@keyframes goldShimmer {
  from {
    background-position: 0% center;
  }
  to {
    background-position: 250% center;
  }
}

.agent-count {
  font-size: 7px;
  color: var(--color-teal);
  text-shadow: 0 0 6px var(--color-teal);
}

/* ── Tasks table ─────────────────────────────────────────── */
.task-table {
  position: absolute;
  display: flex;
  flex-direction: column;
  gap: 8px;
  z-index: 3;
}

/* ── Break room ─────────────────────────────────────────── */
.break-room {
  position: absolute;
  height: 68px;
  border-top: 1px dashed rgba(232, 170, 0, 0.25);
  border-bottom: 1px dashed rgba(232, 170, 0, 0.15);
  background: repeating-linear-gradient(
    45deg,
    rgba(232, 170, 0, 0.03) 0px,
    rgba(232, 170, 0, 0.03) 8px,
    transparent 8px,
    transparent 16px
  );
  z-index: 2;
  pointer-events: none;
}
.break-label {
  position: absolute;
  top: 4px;
  left: 50%;
  transform: translateX(-50%);
  font-size: 6px;
  letter-spacing: 2px;
  color: var(--color-brass-dark);
  text-shadow: 0 0 4px rgba(232, 170, 0, 0.4);
}

/* ── Break room furniture ─────────────────────────────────── */
.br-couch {
  position: absolute;
  left: 4%;
  bottom: 6px;
  pointer-events: none;
}
.couch-back {
  width: 34px;
  height: 7px;
  background: linear-gradient(180deg, #5c3010 0%, #3a1a08 100%);
  border: 1px solid var(--color-copper);
  border-radius: 2px 2px 0 0;
}
.couch-seat {
  position: relative;
  width: 36px;
  height: 9px;
  background: linear-gradient(180deg, #6a3818 0%, #3e1c0a 100%);
  border: 1px solid var(--color-copper);
  border-top: none;
  border-radius: 0 0 2px 2px;
}
.couch-arm {
  position: absolute;
  top: -7px;
  width: 5px;
  height: 16px;
  background: linear-gradient(180deg, #5c3010 0%, #3a1a08 100%);
  border: 1px solid var(--color-copper);
  border-radius: 2px;
}
.couch-arm-l {
  left: -4px;
}
.couch-arm-r {
  right: -4px;
}

.br-table {
  position: absolute;
  bottom: 6px;
  transform: translateX(-50%);
  pointer-events: none;
}
.br-table-top {
  width: 20px;
  height: 4px;
  background: linear-gradient(180deg, var(--color-brass-light) 0%, var(--color-brass-dark) 100%);
  border: 1px solid var(--color-brass);
  border-radius: 1px;
  box-shadow: 0 0 4px rgba(232, 170, 0, 0.3);
}
.br-table-legs {
  display: flex;
  justify-content: space-between;
  padding: 0 2px;
}
.br-table-legs span {
  width: 2px;
  height: 9px;
  background: linear-gradient(180deg, var(--color-brass-dark) 0%, #5a3800 100%);
}
.br-table-1 {
  left: 22%;
}
.br-table-2 {
  left: 40%;
}
.br-table-3 {
  left: 58%;
}

.br-cooler {
  position: absolute;
  left: 76%;
  bottom: 6px;
  transform: translateX(-50%);
  pointer-events: none;
}
.cooler-jug {
  width: 8px;
  height: 10px;
  background: radial-gradient(
    circle at 35% 30%,
    rgba(140, 210, 255, 0.95) 0%,
    rgba(30, 100, 200, 0.8) 70%
  );
  border: 1px solid rgba(68, 153, 255, 0.7);
  border-radius: 3px 3px 4px 4px;
  margin: 0 auto;
  box-shadow: 0 0 5px rgba(68, 153, 255, 0.6);
}
.cooler-body {
  position: relative;
  width: 12px;
  height: 14px;
  background: linear-gradient(180deg, #1a1f2e 0%, #0d1018 100%);
  border: 1px solid rgba(68, 153, 255, 0.4);
  border-radius: 1px;
}
.cooler-spigot {
  position: absolute;
  bottom: 3px;
  left: 50%;
  transform: translateX(-50%);
  width: 6px;
  height: 3px;
  background: var(--color-sky);
  border-radius: 1px;
  box-shadow: 0 0 3px var(--color-sky);
}

.br-board {
  position: absolute;
  right: 3%;
  top: 14px;
  pointer-events: none;
  width: 28px;
  height: 24px;
  background: linear-gradient(180deg, #5c3a10 0%, #3c2208 100%);
  border: 1px solid var(--color-copper);
  border-radius: 1px;
  box-shadow: 0 0 5px rgba(204, 85, 0, 0.3);
}
.board-pin {
  position: absolute;
  top: 4px;
  width: 4px;
  height: 4px;
  border-radius: 50%;
}
.pin-a {
  left: 4px;
  background: var(--color-teal);
  box-shadow: 0 0 3px var(--color-teal);
}
.pin-b {
  left: 50%;
  transform: translateX(-50%);
  background: var(--color-amber);
  box-shadow: 0 0 3px var(--color-amber);
}
.pin-c {
  right: 4px;
  background: var(--color-red);
  box-shadow: 0 0 3px var(--color-red);
}

/* ── Floating agents ──────────────────────────────────────── */
.floating-agent {
  position: absolute;
  z-index: 6;
  transform: translate(-50%, -50%);
  transition:
    left var(--walk-dur, 1.5s) ease-in-out,
    top var(--walk-dur, 1.5s) ease-in-out;
  display: flex;
  flex-direction: column;
  align-items: center;
  pointer-events: none;
}

/* ── Size classes ─────────────────────────────────────────── */
.factory-floor.size-sm .factory-title {
  font-size: 6px;
  letter-spacing: 1.5px;
}
.factory-floor.size-sm .agent-count {
  font-size: 6px;
}
.factory-floor.size-sm :deep(.ambient-gear.g1) {
  font-size: 32px;
  right: 10px;
  top: 50px;
}
.factory-floor.size-sm :deep(.ambient-gear.g2) {
  font-size: 22px;
  right: 36px;
  top: 76px;
}
.factory-floor.size-sm :deep(.row-header) {
  font-size: 6px;
  gap: 6px;
  padding: 3px 6px;
}
.factory-floor.size-sm :deep(.row-state) {
  font-size: 5px;
  padding: 1px 4px;
}

.factory-floor.size-lg .factory-title {
  font-size: 8px;
}
.factory-floor.size-lg .agent-count {
  font-size: 8px;
}
.factory-floor.size-lg :deep(.ambient-gear.g1) {
  font-size: 60px;
}
.factory-floor.size-lg :deep(.ambient-gear.g2) {
  font-size: 38px;
}

/* Row density */
.factory-floor.density-tall :deep(.station-icon) {
  width: 32px;
  height: 32px;
}
.factory-floor.density-tall :deep(.mini-engine) {
  font-size: 26px;
}
.factory-floor.density-tall :deep(.mini-forge) {
  font-size: 22px;
}
.factory-floor.density-tall :deep(.mini-orb) {
  font-size: 20px;
}
.factory-floor.density-tall :deep(.mini-tg-body) {
  width: 28px;
  height: 22px;
}
.factory-floor.density-tall :deep(.mini-tank) {
  width: 26px;
  height: 26px;
}
.factory-floor.density-tall :deep(.mini-book.b1) {
  height: 20px;
}
.factory-floor.density-tall :deep(.mini-book.b2) {
  height: 14px;
}
.factory-floor.density-tall :deep(.mini-book.b3) {
  height: 22px;
}
.factory-floor.density-tall :deep(.mini-book.b4) {
  height: 16px;
}

.factory-floor.density-medium :deep(.station-icon) {
  width: 26px;
  height: 26px;
}
.factory-floor.density-medium :deep(.mini-engine) {
  font-size: 20px;
}
.factory-floor.density-medium :deep(.mini-forge) {
  font-size: 17px;
}
.factory-floor.density-medium :deep(.mini-orb) {
  font-size: 15px;
}

.factory-floor.density-short :deep(.station-icon) {
  width: 20px;
  height: 20px;
}
.factory-floor.density-short :deep(.station-label) {
  display: none;
}
.factory-floor.density-short :deep(.mini-engine) {
  font-size: 16px;
}
.factory-floor.density-short :deep(.mini-forge) {
  font-size: 14px;
}
.factory-floor.density-short :deep(.mini-orb) {
  font-size: 13px;
}
.factory-floor.density-short :deep(.mini-tg-body) {
  width: 18px;
  height: 14px;
}
.factory-floor.density-short :deep(.mini-tank) {
  width: 18px;
  height: 18px;
}
.factory-floor.density-short :deep(.mini-book) {
  width: 3px;
}
.factory-floor.density-short :deep(.mini-book.b1) {
  height: 12px;
}
.factory-floor.density-short :deep(.mini-book.b2) {
  height: 9px;
}
.factory-floor.density-short :deep(.mini-book.b3) {
  height: 14px;
}
.factory-floor.density-short :deep(.mini-book.b4) {
  height: 10px;
}
.factory-floor.density-short :deep(.bench-station) {
  bottom: 10px;
}
.factory-floor.density-short :deep(.bench-rail) {
  bottom: 8px;
}
.factory-floor.density-short :deep(.bench-bolts) {
  bottom: 1px;
  height: 6px;
}
.factory-floor.density-short :deep(.bolt) {
  width: 4px;
  height: 4px;
}
</style>
