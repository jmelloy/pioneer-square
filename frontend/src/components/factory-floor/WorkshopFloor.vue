<template>
  <div class="workshop-floor" ref="floorEl">
    <FactoryAtmosphere />

    <!-- Header -->
    <div class="floor-info">
      <span class="floor-title">⚙ PIONEER SQUARE WORKSHOP ⚙</span>
      <span class="agent-count">Agents: {{ agents.length }}</span>
    </div>

    <!-- Table grid -->
    <div class="table-grid" :style="gridStyle">
      <WorkTable
        v-for="(task, i) in displayTasks"
        :key="task.id"
        :task="task"
        :index="i"
        :state-label="stateLabel"
        class="grid-table"
        :style="tableStyle(i)"
        @click="onTableClick"
      />
      <!-- Empty placeholder slots to fill the current row -->
      <div
        v-for="i in emptySlots"
        :key="`empty-${i}`"
        class="grid-table empty-slot"
        :style="emptySlotStyle(displayTasks.length + i - 1)"
      >
        <WorkTable :task="null" :index="displayTasks.length + i" :state-label="stateLabel" />
      </div>
    </div>

    <!-- Break room below the grid -->
    <div class="break-room" :style="breakRoomStyle">
      <div class="break-label">⏚ BREAK ROOM ⏚</div>
      <div class="br-furniture br-bulletin">📋 BOARD</div>
      <div class="br-furniture br-watercooler">💧 H₂O</div>
      <div class="br-furniture br-couch">🛋 COUCH</div>
    </div>

    <!-- Floating agents — walk up to their table -->
    <div
      v-for="agent in agents"
      :key="agent.id"
      class="floating-agent"
      :style="agentStyle(agent)"
    >
      <AgentAvatar :agent="agent" :walking="!!walking[agent.id]" />
    </div>

    <TickerTape :messages="tickerMessages" />
  </div>
</template>

<script setup lang="ts">
import { computed, ref, reactive, watch, onMounted, onUnmounted } from 'vue'
import { useAgentsStore } from '../../stores/agents'
import { useTasksStore } from '../../stores/tasks'
import type { Agent, Task } from '../../types'
import AgentAvatar from '../AgentAvatar.vue'
import FactoryAtmosphere from './FactoryAtmosphere.vue'
import TickerTape from './TickerTape.vue'
import WorkTable from './WorkTable.vue'

const agentsStore = useAgentsStore()
const tasksStore = useTasksStore()

const agents = computed(() => agentsStore.agents.filter((a) => a.state !== 'offline'))

// ── Layout constants ───────────────────────────────────────
const TABLE_W = 200
const TABLE_H = 148
const TABLE_GAP = 14
const GRID_PAD_X = 20
const GRID_PAD_TOP = 52   // below the header strip
const BREAK_ROOM_H = 68
const BREAK_PAD = 16
const MAX_TASKS = 20

// ── Floor measurement ──────────────────────────────────────
const floorEl = ref<HTMLElement | null>(null)
const floorW = ref(900)
const floorH = ref(600)

function updateSize() {
  if (floorEl.value) {
    const w = floorEl.value.clientWidth
    const h = floorEl.value.clientHeight
    if (w > 0) floorW.value = w
    if (h > 0) floorH.value = h
  }
}

let resizeObs: ResizeObserver | null = null
onMounted(() => {
  if (floorEl.value) {
    resizeObs = new ResizeObserver(updateSize)
    resizeObs.observe(floorEl.value)
  }
  updateSize()
  syncAll()
})
onUnmounted(() => {
  resizeObs?.disconnect()
  Object.values(wanderTimers).forEach(clearTimeout)
  Object.values(walkTimers).forEach(clearTimeout)
})

// ── Grid geometry ──────────────────────────────────────────
const gridCols = computed(() => {
  const usable = floorW.value - GRID_PAD_X * 2
  const cols = Math.max(1, Math.floor((usable + TABLE_GAP) / (TABLE_W + TABLE_GAP)))
  return Math.min(cols, 5)
})

const displayTasks = computed<Task[]>(() =>
  tasksStore.liveTasks
    .filter((t) => t.worker_id && t.worker_id !== 'foreman')
    .slice(0, MAX_TASKS),
)

// Always show at least enough slots to fill one row
const totalSlots = computed(() => Math.max(gridCols.value, displayTasks.value.length))

const gridRows = computed(() => Math.ceil(totalSlots.value / gridCols.value))

const emptySlots = computed(() => {
  const filled = displayTasks.value.length
  const needed = gridRows.value * gridCols.value - filled
  return Math.max(0, needed)
})

function colOf(i: number) { return i % gridCols.value }
function rowOf(i: number) { return Math.floor(i / gridCols.value) }

function tableLeft(i: number) {
  return GRID_PAD_X + colOf(i) * (TABLE_W + TABLE_GAP)
}
function tableTop(i: number) {
  return GRID_PAD_TOP + rowOf(i) * (TABLE_H + TABLE_GAP)
}

// Grid container style — sized to contain all rows
const gridStyle = computed(() => ({
  position: 'absolute' as const,
  top: '0',
  left: '0',
  width: `${floorW.value}px`,
  height: `${gridRows.value * (TABLE_H + TABLE_GAP) + GRID_PAD_TOP + 8}px`,
}))

function tableStyle(i: number) {
  return {
    position: 'absolute' as const,
    left: `${tableLeft(i)}px`,
    top: `${tableTop(i)}px`,
    width: `${TABLE_W}px`,
    height: `${TABLE_H}px`,
  }
}

function emptySlotStyle(i: number) {
  return tableStyle(i)
}

// Break room sits below all grid rows
const breakRoomTop = computed(
  () => GRID_PAD_TOP + gridRows.value * (TABLE_H + TABLE_GAP) + BREAK_PAD,
)

const breakRoomStyle = computed(() => ({
  top: `${breakRoomTop.value}px`,
  left: `${GRID_PAD_X}px`,
  width: `${floorW.value - GRID_PAD_X * 2}px`,
  height: `${BREAK_ROOM_H}px`,
}))

// ── Agent choreography ─────────────────────────────────────
// Positions keyed by agent.id
const positions = reactive<Record<string, { x: number; y: number }>>({})
const walking = reactive<Record<string, boolean>>({})
const duration = reactive<Record<string, number>>({})
const walkTimers: Record<string, ReturnType<typeof setTimeout>> = {}
const wanderTimers: Record<string, ReturnType<typeof setTimeout>> = {}

// Center-front of a grid table (where the worker stands at the table)
function tableCenterPos(taskIndex: number) {
  return {
    x: tableLeft(taskIndex) + TABLE_W / 2,
    y: tableTop(taskIndex) + TABLE_H * 0.82,   // near front edge
  }
}

function randomBreakPos(agent: Agent) {
  const xMin = GRID_PAD_X + 30
  const xMax = GRID_PAD_X + floorW.value - GRID_PAD_X * 2 - 30
  const idx = agents.value.findIndex((a) => a.id === agent.id)
  const total = Math.max(agents.value.length, 1)
  const lane = total > 1 ? (idx + 0.5 + (Math.random() - 0.5) * 0.7) / total : 0.5
  return {
    x: xMin + Math.max(0, Math.min(1, lane)) * (xMax - xMin),
    y: breakRoomTop.value + 20 + Math.random() * (BREAK_ROOM_H - 30),
  }
}

function isActive(agent: Agent) {
  return !['idle', 'offline'].includes(agent.state)
}

function taskIndexForAgent(agent: Agent): number | null {
  if (!agent.workerId) return null
  const idx = displayTasks.value.findIndex((t) => t.worker_id === agent.workerId)
  return idx >= 0 ? idx : null
}

function targetFor(agent: Agent) {
  const tIdx = taskIndexForAgent(agent)
  if (tIdx !== null && isActive(agent)) {
    return tableCenterPos(tIdx)
  }
  return randomBreakPos(agent)
}

function targetKey(agent: Agent) {
  const tIdx = taskIndexForAgent(agent)
  if (tIdx !== null && isActive(agent)) return `task-${tIdx}:${agent.state}`
  return `break:${agent.id}`
}

function moveAgent(agent: Agent, target: { x: number; y: number }) {
  const id = agent.id
  const cur = positions[id] ?? target
  const dist = Math.hypot(target.x - cur.x, target.y - cur.y)
  const dur = Math.max(0.6, dist / 120)
  duration[id] = dur
  walking[id] = true
  positions[id] = target

  clearTimeout(walkTimers[id])
  walkTimers[id] = setTimeout(
    () => {
      walking[id] = false
      const a = agents.value.find((x) => x.id === id)
      if (a && !isActive(a)) scheduleWander(a)
    },
    dur * 1000 + 60,
  )
}

const prevKeys: Record<string, string> = {}

function syncAgent(agent: Agent) {
  const key = targetKey(agent)
  if (prevKeys[agent.id] === key && positions[agent.id]) return
  prevKeys[agent.id] = key
  if (!positions[agent.id]) {
    positions[agent.id] = randomBreakPos(agent)
  }
  clearTimeout(wanderTimers[agent.id])
  moveAgent(agent, targetFor(agent))
}

function syncAll() {
  agents.value.forEach(syncAgent)
}

function scheduleWander(agent: Agent) {
  const delay = 3500 + Math.random() * 4000
  clearTimeout(wanderTimers[agent.id])
  wanderTimers[agent.id] = setTimeout(() => {
    const a = agents.value.find((x) => x.id === agent.id)
    if (!a || isActive(a)) return
    positions[agent.id] = randomBreakPos(a)
    duration[agent.id] = 1.2
    walking[agent.id] = true
    clearTimeout(walkTimers[agent.id])
    walkTimers[agent.id] = setTimeout(() => {
      walking[agent.id] = false
      scheduleWander(a)
    }, 1260)
  }, delay)
}

watch(
  () => agents.value.map((a) => `${a.id}:${a.state}:${a.workerId}`).join(','),
  syncAll,
)
watch(displayTasks, syncAll)

function agentStyle(agent: Agent) {
  const pos = positions[agent.id] ?? { x: GRID_PAD_X + 60, y: GRID_PAD_TOP + 60 }
  const dur = duration[agent.id] ?? 1.5
  return {
    left: `${pos.x}px`,
    top: `${pos.y}px`,
    '--walk-dur': `${dur}s`,
  }
}

// ── Interaction ────────────────────────────────────────────
function onTableClick(task: Task) {
  tasksStore.selectTask(task.id)
}

// ── Ticker ─────────────────────────────────────────────────
const tickerMessages = computed(() => {
  const msgs: string[] = []
  agents.value.forEach((a) => {
    const label = a.activity ? `${a.state.toUpperCase()} [${a.activity}]` : a.state.toUpperCase()
    msgs.push(`${a.name}: ${label}`)
  })
  displayTasks.value
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
.workshop-floor {
  width: 100%;
  height: 100%;
  background: linear-gradient(180deg, #0d0600 0%, #120900 50%, #1a0e00 100%);
  position: relative;
  overflow-y: auto;
  overflow-x: hidden;
  font-family: var(--font-pixel);
}

/* Header */
.floor-info {
  position: sticky;
  top: 8px;
  left: 50%;
  transform: translateX(-50%);
  display: inline-flex;
  gap: 20px;
  align-items: center;
  background: rgba(12, 7, 0, 0.88);
  border: 1px solid var(--color-brass);
  padding: 4px 14px;
  z-index: 20;
  box-shadow:
    0 0 12px rgba(232, 170, 0, 0.3),
    inset 0 0 6px rgba(232, 170, 0, 0.06);
  width: max-content;
  margin: 0 auto;
}

.floor-title {
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
  from { background-position: 0% center; }
  to { background-position: 250% center; }
}

.agent-count {
  font-size: 7px;
  color: var(--color-teal);
  text-shadow: 0 0 6px var(--color-teal);
}

/* Grid container (absolutely positioned children) */
.table-grid {
  position: relative;
  z-index: 3;
}

.grid-table {
  border-radius: 4px;
}

.empty-slot {
  opacity: 0.3;
  pointer-events: none;
}

/* Break room */
.break-room {
  position: absolute;
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

.br-furniture {
  position: absolute;
  background: rgba(12, 7, 0, 0.85);
  border: 1px solid rgba(200, 140, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 6px;
  letter-spacing: 1px;
  color: var(--color-brass-dark);
  gap: 2px;
  white-space: nowrap;
}
.br-bulletin  { left: 2%; top: 17px; width: 42px; height: 22px; border-left: 2px solid rgba(232,170,0,0.55); }
.br-watercooler { right: 2%; top: 17px; width: 36px; height: 22px; }
.br-couch { left: 30%; bottom: 5px; width: 58px; height: 16px; font-size: 11px; background: rgba(60,30,0,0.7); border-color: rgba(160,90,0,0.5); gap: 3px; }

/* Floating agents — CSS transitions create the walk-up animation */
.floating-agent {
  position: absolute;
  z-index: 10;
  transform: translate(-50%, -100%);
  transition:
    left var(--walk-dur, 1.5s) ease-in-out,
    top var(--walk-dur, 1.5s) ease-in-out;
  display: flex;
  flex-direction: column;
  align-items: center;
  pointer-events: none;
}
</style>
