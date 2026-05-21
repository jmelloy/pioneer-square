<template>
  <div class="factory-floor" ref="floorEl">
    <!-- Pixel-art workshop. The stage keeps the image's aspect ratio and is
         centred; everything inside is positioned as a fraction of it. -->
    <div class="floor-stage" :style="stageStyle">
      <!-- Coordinate-tuning overlay: walkable polygon, patrol loop, POIs. -->
      <svg
        v-if="SHOW_FLOOR_DEBUG"
        class="floor-debug"
        viewBox="0 0 100 100"
        preserveAspectRatio="none"
      >
        <polygon :points="debugPolygon" />
        <polyline class="patrol" :points="debugPatrol" />
        <circle
          v-for="p in POINTS_OF_INTEREST"
          :key="p.id"
          :cx="p.x * 100"
          :cy="p.y * 100"
          r="1.6"
        />
      </svg>

      <!-- Work stations — one per active task. -->
      <div
        v-for="station in activeStations"
        :key="station.task.id"
        class="work-station occupied"
        :style="`left: ${station.x * 100}%; top: ${station.y * 100}%; z-index: ${zIndex(station.y)}`"
      >
        <div class="station-monitor">
          <div class="monitor-screen">
            <div class="screen-active">
              <div v-for="l in 3" :key="l" class="screen-line"></div>
            </div>
          </div>
        </div>
        <div class="station-label">{{ truncate(station.task.name || station.task.id, 12) }}</div>
        <div class="task-badge" :class="`state-${station.task.state}`">
          {{ stateLabel(station.task.state) }}
        </div>
      </div>

      <!-- Agents — walk to their task station, or patrol the loop around
           the tables when idle, leaning into their direction of travel. -->
      <div
        v-for="agent in agents"
        :key="agent.id"
        class="floating-agent"
        :style="`left: ${agentPos(agent.id).x * 100}%; top: ${
          agentPos(agent.id).y * 100
        }%; z-index: ${zIndex(agentPos(agent.id).y)}`"
      >
        <div class="agent-tilt" :style="`transform: rotate(${agentTilt(agent.id)}deg)`">
          <AgentAvatar :agent="agent" :walking="isWalking(agent.id)" />
        </div>
        <div class="agent-nametag">{{ agent.name }}</div>
      </div>
    </div>

    <!-- Info overlay -->
    <div class="factory-info">
      <span class="factory-title">⚙ PIONEER SQUARE WORKSHOP ⚙</span>
      <span class="agent-count">Agents: {{ agents.length }}</span>
    </div>

    <!-- Ticker tape -->
    <div class="ticker-tape">
      <div class="ticker-content">
        <span v-for="(msg, i) in tickerMessages" :key="i" class="ticker-msg"> ⚙ {{ msg }} </span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch, onMounted, onUnmounted } from 'vue'
import { useAgentsStore } from '../stores/agents'
import { useTasksStore } from '../stores/tasks'
import AgentAvatar from './AgentAvatar.vue'
import layout from './factory-layout.json'
import type { Agent, Task } from '../types'

const agentsStore = useAgentsStore()
const tasksStore = useTasksStore()
const agents = computed(() => agentsStore.agents.filter((a) => a.state !== 'offline'))

// Flip to true to render the walkable polygon + POI markers while tuning.
const SHOW_FLOOR_DEBUG = false

// ─── Image-derived layout ──────────────────────────────────────────────────
// Coordinates are fractions (0-1) of the floor stage. They live in
// factory-layout.json so the same numbers drive scripts/preview_factory_layout.py.
// `walkableFloor` is the open tiled area: its first edges are the two back
// wall bases, the rest hug the machinery in the front corners.

const STAGE_RATIO = layout.image.width / layout.image.height
const FLOOR_POLYGON = layout.walkableFloor as [number, number][]
const POINTS_OF_INTEREST = layout.pointsOfInterest
const STATION_POSITIONS = layout.stationSlots as [number, number][]
const GRAVITATE_RADIUS = layout.gravitateRadius
const MAX_STATIONS = STATION_POSITIONS.length

// Ordered loop of waypoints that hugs the open floor around the two tables.
// Idle robots step along it (bouncing at the ends) so they circle the tables
// rather than standing on them.
const PATROL_PATH = layout.patrolPath as [number, number][]
const PATROL_JITTER = layout.patrolJitter
const MAX_TILT = 9

const FLOOR_BBOX = {
  minX: Math.min(...FLOOR_POLYGON.map((p) => p[0])),
  maxX: Math.max(...FLOOR_POLYGON.map((p) => p[0])),
  minY: Math.min(...FLOOR_POLYGON.map((p) => p[1])),
  maxY: Math.max(...FLOOR_POLYGON.map((p) => p[1])),
}

const debugPolygon = FLOOR_POLYGON.map(([x, y]) => `${x * 100},${y * 100}`).join(' ')
const debugPatrol = PATROL_PATH.map(([x, y]) => `${x * 100},${y * 100}`).join(' ')

function pointInFloor(x: number, y: number) {
  let inside = false
  const p = FLOOR_POLYGON
  for (let i = 0, j = p.length - 1; i < p.length; j = i++) {
    const [xi, yi] = p[i]
    const [xj, yj] = p[j]
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) {
      inside = !inside
    }
  }
  return inside
}

// ─── Stage sizing ──────────────────────────────────────────────────────────
const floorEl = ref<HTMLElement | null>(null)
const stage = reactive({ w: 0, h: 0 })
let _ro: ResizeObserver | null = null

const stageStyle = computed(() => ({ width: `${stage.w}px`, height: `${stage.h}px` }))

function measureStage() {
  const el = floorEl.value
  if (!el) return
  const cw = el.clientWidth
  const ch = el.clientHeight
  if (cw <= 0 || ch <= 0) return
  if (cw / ch > STAGE_RATIO) {
    stage.h = ch
    stage.w = ch * STAGE_RATIO
  } else {
    stage.w = cw
    stage.h = cw / STAGE_RATIO
  }
}

// ─── Work stations ─────────────────────────────────────────────────────────
const activeTasks = computed<Task[]>(() =>
  tasksStore.tasks
    .filter(
      (t) =>
        !['done', 'failed', 'cancelled'].includes(t.state) &&
        t.worker_id &&
        t.worker_id !== 'foreman',
    )
    .slice(0, MAX_STATIONS),
)

interface Station {
  x: number
  y: number
  task: Task
}

const activeStations = computed<Station[]>(() =>
  activeTasks.value.map((task, i) => ({
    x: STATION_POSITIONS[i][0],
    y: STATION_POSITIONS[i][1],
    task,
  })),
)

// ─── Agent positions ───────────────────────────────────────────────────────
// Positions are fractions of the stage. `walking` is set briefly while an
// agent moves so RobotWorker runs its walk animation. `tilt` is a small lean
// in the agent's current direction of travel.
const agentPositions = reactive<Record<string, { x: number; y: number }>>({})
const walking = reactive<Record<string, boolean>>({})
const tilt = reactive<Record<string, number>>({})
const walkTimers: Record<string, ReturnType<typeof setTimeout>> = {}

// Each patrolling agent tracks where it is on PATROL_PATH and which way it
// is heading; it reverses at the ends so the loop never teleports.
const patrolState = reactive<Record<string, { idx: number; dir: number }>>({})

function agentPos(agentId: string) {
  return agentPositions[agentId] || { x: 0.4, y: 0.64 }
}

function isWalking(agentId: string) {
  return !!walking[agentId]
}

function agentTilt(agentId: string) {
  return tilt[agentId] || 0
}

function zIndex(y: number) {
  return Math.round(y * 1000)
}

function patrolStep(agentId: string) {
  let st = patrolState[agentId]
  if (!st) {
    st = {
      idx: Math.floor(Math.random() * PATROL_PATH.length),
      dir: Math.random() < 0.5 ? 1 : -1,
    }
    patrolState[agentId] = st
  }
  st.idx += st.dir
  if (st.idx > PATROL_PATH.length - 1) {
    st.dir = -1
    st.idx = PATROL_PATH.length - 2
  } else if (st.idx < 0) {
    st.dir = 1
    st.idx = 1
  }
  const [x, y] = PATROL_PATH[st.idx]
  return jitterAround({ x, y }, PATROL_JITTER)
}

function jitterAround(p: { x: number; y: number }, radius = GRAVITATE_RADIUS) {
  for (let i = 0; i < 20; i++) {
    const x = p.x + (Math.random() - 0.5) * 2 * radius
    const y = p.y + (Math.random() - 0.5) * 2 * radius
    if (pointInFloor(x, y)) return { x, y }
  }
  return { x: p.x, y: p.y }
}

function poiTarget() {
  const poi = POINTS_OF_INTEREST[Math.floor(Math.random() * POINTS_OF_INTEREST.length)]
  return jitterAround(poi)
}

function randomFloorPos() {
  for (let i = 0; i < 40; i++) {
    const x = FLOOR_BBOX.minX + Math.random() * (FLOOR_BBOX.maxX - FLOOR_BBOX.minX)
    const y = FLOOR_BBOX.minY + Math.random() * (FLOOR_BBOX.maxY - FLOOR_BBOX.minY)
    if (pointInFloor(x, y)) return { x, y }
  }
  return { x: 0.4, y: 0.64 }
}

// An agent is "at work" when it owns one of the visible task stations.
function stationForAgent(agent: Agent): Station | null {
  if (!agent.taskId) return null
  return activeStations.value.find((s) => s.task.id === agent.taskId) || null
}

function isAgentAtWork(agent: Agent) {
  return !!stationForAgent(agent) && !['idle', 'offline'].includes(agent.state)
}

function moveTo(agentId: string, pos: { x: number; y: number }) {
  const cur = agentPositions[agentId]
  if (cur && cur.x === pos.x && cur.y === pos.y) return
  if (cur) {
    // Lean into the direction of travel, measured in screen pixels so the
    // stage's portrait aspect ratio doesn't skew the angle.
    const dx = (pos.x - cur.x) * (stage.w || STAGE_RATIO)
    const dy = (pos.y - cur.y) * (stage.h || 1)
    const dist = Math.hypot(dx, dy)
    if (dist > 0) {
      tilt[agentId] = Math.max(-MAX_TILT, Math.min(MAX_TILT, (dx / dist) * MAX_TILT))
    }
  }
  agentPositions[agentId] = pos
  walking[agentId] = true
  clearTimeout(walkTimers[agentId])
  walkTimers[agentId] = setTimeout(() => {
    walking[agentId] = false
  }, 2400)
}

function syncPositions() {
  agents.value.forEach((agent) => {
    if (isAgentAtWork(agent)) {
      const station = stationForAgent(agent)!
      moveTo(agent.id, { x: station.x, y: station.y + 0.07 })
    } else if (!agentPositions[agent.id]) {
      agentPositions[agent.id] = patrolStep(agent.id)
    }
  })
}

// Cycle through idle agents one at a time so they don't all move at once.
let walkIdx = 0
let walkTimer: ReturnType<typeof setInterval> | null = null

function tickWalk() {
  const idle = agents.value.filter((a) => !isAgentAtWork(a))
  if (idle.length === 0) return
  const agent = idle[walkIdx % idle.length]
  walkIdx++
  // Mostly patrol the loop around the tables; occasionally drift to a
  // wall feature or roam the open floor.
  const roll = Math.random()
  const target = roll < 0.85 ? patrolStep(agent.id) : roll < 0.95 ? poiTarget() : randomFloorPos()
  moveTo(agent.id, target)
}

onMounted(() => {
  measureStage()
  if (floorEl.value) {
    _ro = new ResizeObserver(measureStage)
    _ro.observe(floorEl.value)
  }
  syncPositions()
  walkTimer = setInterval(tickWalk, 3200)
})

onUnmounted(() => {
  if (walkTimer) clearInterval(walkTimer)
  Object.values(walkTimers).forEach(clearTimeout)
  _ro?.disconnect()
})

watch(agents, syncPositions, { deep: true })
watch(activeStations, syncPositions)

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
  if (msgs.length === 0) {
    msgs.push('AWAITING WORKERS', 'SYSTEMS NOMINAL', 'BOILER PRESSURE: 87 PSI')
  }
  return msgs
})

function truncate(str: string | undefined, len: number) {
  if (!str) return ''
  return str.length > len ? str.slice(0, len) + '…' : str
}

function stateLabel(state: string) {
  return tasksStore.stateLabel(state)
}
</script>

<style scoped>
.factory-floor {
  width: 100%;
  height: 100%;
  background: #0b0500;
  position: relative;
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: var(--font-pixel);
}

.floor-stage {
  position: relative;
  container-type: size;
  background-image: url('../assets/factory-floor.jpg');
  background-size: 100% 100%;
  background-repeat: no-repeat;
  image-rendering: pixelated;
  box-shadow:
    0 0 24px rgba(0, 0, 0, 0.8),
    inset 0 0 60px rgba(0, 0, 0, 0.35);
}

.floor-debug {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  z-index: 900;
}
.floor-debug polygon {
  fill: rgba(0, 255, 120, 0.16);
  stroke: rgba(0, 255, 120, 0.9);
  stroke-width: 0.4;
}
.floor-debug circle {
  fill: rgba(255, 60, 60, 0.95);
}
.floor-debug polyline.patrol {
  fill: none;
  stroke: rgba(120, 190, 255, 0.95);
  stroke-width: 0.6;
  stroke-dasharray: 1.4 1;
}

/* ── Work stations ───────────────────────────────────────────────────────── */
.work-station {
  position: absolute;
  width: 22cqw;
  transform: translate(-50%, -50%);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 1cqw;
  pointer-events: none;
}
.station-monitor {
  width: 13cqw;
  height: 10cqw;
  background: #0d0600;
  border: 2px solid var(--color-brass-dark);
  border-radius: 2px;
  display: flex;
  overflow: hidden;
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.6);
}
.monitor-screen {
  flex: 1;
  background: #080400;
  border: 2px solid #1c1000;
  padding: 1.4cqw;
  display: flex;
  align-items: center;
  justify-content: center;
}
.screen-active {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 0.8cqw;
}
.screen-line {
  height: 0.9cqw;
  opacity: 0.85;
  animation: screenScroll 2s infinite linear;
  border-radius: 1px;
}
.screen-line:nth-child(1) {
  background: var(--color-teal);
}
.screen-line:nth-child(2) {
  background: var(--color-sky);
  animation-delay: -0.7s;
  opacity: 0.5;
}
.screen-line:nth-child(3) {
  background: var(--color-green);
  animation-delay: -1.4s;
  opacity: 0.3;
  width: 60%;
}
@keyframes screenScroll {
  0%,
  100% {
    opacity: 0.85;
  }
  50% {
    opacity: 0.3;
  }
}

.station-label {
  font-size: 1.7cqw;
  color: var(--color-brass-light);
  letter-spacing: 1px;
  max-width: 22cqw;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-shadow: 0 0 4px rgba(0, 0, 0, 0.9);
}

.task-badge {
  font-size: 1.7cqw;
  padding: 0.3cqw 1cqw;
  border-radius: 2px;
  letter-spacing: 1px;
  text-transform: uppercase;
  background: rgba(12, 7, 0, 0.85);
}
.state-working {
  color: var(--color-green);
  text-shadow: 0 0 4px var(--color-green);
}
.state-pending {
  color: var(--color-text-dim);
}
.state-planning {
  color: var(--color-sky);
  text-shadow: 0 0 4px var(--color-sky);
}
.state-awaiting-review {
  color: var(--color-amber);
  text-shadow: 0 0 4px var(--color-amber);
}
.state-followup {
  color: var(--color-orange);
  text-shadow: 0 0 4px var(--color-orange);
}
.state-failed {
  color: var(--color-red);
  text-shadow: 0 0 4px var(--color-red);
}
.state-done {
  color: var(--color-teal);
  text-shadow: 0 0 4px var(--color-teal);
}
.state-cancelled {
  color: var(--color-red);
  text-shadow: 0 0 4px var(--color-red);
}

/* ── Agents ──────────────────────────────────────────────────────────────── */
.floating-agent {
  position: absolute;
  transform: translate(-50%, -82%);
  transition:
    left 2.4s ease-in-out,
    top 2.4s ease-in-out;
  display: flex;
  flex-direction: column;
  align-items: center;
  pointer-events: none;
}
.floating-agent :deep(.robot-sprite) {
  width: 9cqw;
  height: auto;
}
.agent-tilt {
  display: flex;
  justify-content: center;
  transform-origin: 50% 88%;
  transition: transform 1.2s ease-in-out;
}
.agent-nametag {
  font-size: 1.7cqw;
  color: var(--color-brass-light);
  margin-top: 0.4cqw;
  white-space: nowrap;
  text-shadow:
    0 0 4px rgba(0, 0, 0, 0.95),
    0 0 6px rgba(232, 170, 0, 0.4);
  letter-spacing: 1px;
}

/* ── HUD ─────────────────────────────────────────────────────────────────── */
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

.ticker-tape {
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  height: 28px;
  background: var(--color-bg-secondary);
  border-top: 2px solid var(--color-brass-dark);
  overflow: hidden;
  display: flex;
  align-items: center;
  z-index: 10;
}
.ticker-content {
  display: flex;
  animation: tickerScroll 20s linear infinite;
  white-space: nowrap;
}
.ticker-msg {
  font-size: 8px;
  color: var(--color-amber);
  margin-right: 60px;
  letter-spacing: 1px;
  text-shadow: 0 0 4px rgba(255, 204, 0, 0.5);
}
.ticker-msg:nth-child(3n + 1) {
  color: var(--color-amber);
}
.ticker-msg:nth-child(3n + 2) {
  color: var(--color-teal);
  text-shadow: 0 0 4px rgba(0, 187, 170, 0.5);
}
.ticker-msg:nth-child(3n) {
  color: var(--color-gold);
  text-shadow: 0 0 4px rgba(255, 214, 68, 0.5);
}
@keyframes tickerScroll {
  from {
    transform: translateX(100vw);
  }
  to {
    transform: translateX(-100%);
  }
}
</style>
