<template>
  <div class="factory-floor" ref="floorEl">
    <!-- Dust motes / sparkles floating in the window light -->
    <div class="sparkle-field">
      <div
        v-for="n in 14"
        :key="`sp${n}`"
        class="sparkle"
        :style="`left: ${(n * 73 + 11) % 93}%; top: ${(n * 59 + 17) % 80}%; font-size: ${
          8 + (n % 3) * 3
        }px; animation-delay: ${((n * 0.37) % 2.8).toFixed(1)}s; animation-duration: ${
          2.5 + (n % 3) * 0.8
        }s;`"
      >
        {{ ['✦', '★', '✧', '⋆', '✩'][n % 5] }}
      </div>
    </div>

    <!-- Steam vents (atmospheric — match the steaming apparatus in the bg) -->
    <div class="steam-vent vent1">
      <div v-for="n in 5" :key="n" class="steam-particle" :style="`--delay: ${n * 0.35}s`"></div>
    </div>
    <div class="steam-vent vent2">
      <div v-for="n in 5" :key="n" class="steam-particle" :style="`--delay: ${n * 0.45}s`"></div>
    </div>

    <!-- Work stations — perspective-scaled by row depth -->
    <div
      v-for="(station, i) in visibleStations"
      :key="i"
      class="work-station"
      :class="{ occupied: station.task }"
      :style="`left: ${station.x}px; top: ${station.y}px; --scale: ${station.scale}`"
    >
      <div class="station-desk">
        <img :src="tableImg" class="station-table-img" alt="" />
      </div>
      <div class="station-label">
        {{ station.task ? truncate(station.task.name || station.task.id, 12) : `WS-${i + 1}` }}
      </div>
      <div v-if="station.task" class="task-badge" :class="`state-${station.task.state}`">
        {{ stateLabel(station.task.state) }}
      </div>
    </div>

    <!-- Floating agents — walk to their task station or wander when idle -->
    <div
      v-for="agent in agents"
      :key="agent.id"
      class="floating-agent"
      :style="`left: ${agentPos(agent.id).x}px; top: ${agentPos(agent.id).y}px`"
    >
      <AgentAvatar :agent="agent" :walking="isWalking(agent.id)" />
      <div class="agent-nametag">{{ agent.name }}</div>
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
import type { Agent, Task } from '../types'
import tableImg from '../assets/table.png'

const agentsStore = useAgentsStore()
const tasksStore = useTasksStore()
const agents = computed(() => agentsStore.agents.filter((a) => a.state !== 'offline'))

const floorEl = ref<HTMLElement | null>(null)
const containerWidth = ref(640)
const containerHeight = ref(500)
let _ro: ResizeObserver | null = null

interface StationPos {
  x: number
  y: number
  scale: number
}

// Background image geometry (workshop-bg.jpg, 1360×768):
//   floor starts at image y=400
//   floor trapezoid: 525px wide centred at x=680 at y=400, 700px wide at y=768
const BG_W = 1360, BG_H = 768
const TRAP_TOP_Y = 400, TRAP_MID_X = 680
const TRAP_TOP_HALF = 262.5  // 525/2
const TRAP_BOT_HALF = 350    // 700/2
const TRAP_RANGE = BG_H - TRAP_TOP_Y // 368

// Return the left/right x bounds (panel px) of the floor trapezoid at a given panel y.
function floorBounds(panelY: number, panelW: number, panelH: number) {
  const scale = Math.max(panelW / BG_W, panelH / BG_H)
  const xOff  = (BG_W * scale - panelW) / 2
  const imgY  = panelY / scale
  const t     = Math.max(0, Math.min(1, (imgY - TRAP_TOP_Y) / TRAP_RANGE))
  const half  = TRAP_TOP_HALF + t * (TRAP_BOT_HALF - TRAP_TOP_HALF)
  return {
    left:  Math.max(0,      Math.round((TRAP_MID_X - half) * scale - xOff)),
    right: Math.min(panelW, Math.round((TRAP_MID_X + half) * scale - xOff)),
  }
}

// 3 tables per side wall, placed exactly against the trapezoid walls.
const stationPositions = computed((): StationPos[] => {
  const w = containerWidth.value
  const h = containerHeight.value
  const tw = (s: number) => Math.round(s * 200)

  // y bands: start just past the floor line, stagger toward viewer
  // Offset -50 so the table bottoms stay visible rather than clipping off screen
  const floorY = Math.round((TRAP_TOP_Y / BG_H) * h) // where floor meets wall
  const yBack  = floorY + Math.round(h * 0.04) - 50
  const yMid   = floorY + Math.round(h * 0.16) - 50
  const yFront = floorY + Math.round(h * 0.28) - 50

  if (w < 600) {
    const bb = floorBounds(yBack,  w, h)
    const fb = floorBounds(yFront, w, h)
    return [
      { x: bb.left,               y: yBack,  scale: 0.68 },
      { x: bb.right - tw(0.68),   y: yBack,  scale: 0.68 },
      { x: fb.left,               y: yFront, scale: 0.92 },
      { x: fb.right - tw(0.92),   y: yFront, scale: 0.92 },
    ]
  }

  const bb = floorBounds(yBack,  w, h)
  const mb = floorBounds(yMid,   w, h)
  const fb = floorBounds(yFront, w, h)
  return [
    { x: bb.left,              y: yBack,  scale: 0.66 }, // left-back
    { x: mb.left,              y: yMid,   scale: 0.84 }, // left-mid
    { x: fb.left,              y: yFront, scale: 1.04 }, // left-front
    { x: bb.right - tw(0.66),  y: yBack,  scale: 0.66 }, // right-back
    { x: mb.right - tw(0.84),  y: yMid,   scale: 0.84 }, // right-mid
    { x: fb.right - tw(1.04),  y: yFront, scale: 1.04 }, // right-front
  ]
})

// Agents wander the open stone floor between the two side-wall table columns.
const WALK_AREA = computed(() => {
  const w = containerWidth.value
  const h = containerHeight.value
  const floorY = Math.round((TRAP_TOP_Y / BG_H) * h)
  const yMin   = floorY + Math.round(h * 0.10)
  const yMax   = Math.round(h * 0.88)
  // Centre corridor: between the back-table right edge and left edge of right tables
  const midBounds = floorBounds(yMin + (yMax - yMin) / 2, w, h)
  const xMin = Math.round(midBounds.left  + (midBounds.right - midBounds.left) * 0.20)
  const xMax = Math.round(midBounds.right - (midBounds.right - midBounds.left) * 0.20)
  return { xMin, xMax, yMin, yMax }
})

const MAX_STATIONS = computed(() => (containerWidth.value < 480 ? 4 : 6))

interface Station extends StationPos {
  task: Task | null
}

const activeTasks = computed<Task[]>(() =>
  tasksStore.tasks
    .filter(
      (t) =>
        !['done', 'failed', 'cancelled'].includes(t.state) &&
        t.worker_id &&
        t.worker_id !== 'foreman',
    )
    .slice(0, MAX_STATIONS.value),
)

const visibleStations = computed<Station[]>(() =>
  stationPositions.value.map((pos, i) => ({ ...pos, task: activeTasks.value[i] || null })),
)

const agentPositions = reactive<Record<string, { x: number; y: number }>>({})
const walking = reactive<Record<string, boolean>>({})
const walkTimers: Record<string, ReturnType<typeof setTimeout>> = {}

function agentPos(agentId: string) {
  return agentPositions[agentId] || { x: 220, y: 360 }
}

function isWalking(agentId: string) {
  return !!walking[agentId]
}

function randomPos() {
  const { xMin, xMax, yMin, yMax } = WALK_AREA.value
  return {
    x: xMin + Math.random() * (xMax - xMin),
    y: yMin + Math.random() * (yMax - yMin),
  }
}

function stationForAgent(agent: Agent): Station | null {
  if (!agent.taskId) return null
  return visibleStations.value.find((s) => s.task && s.task.id === agent.taskId) || null
}

function isAgentAtWork(agent: Agent) {
  return !!stationForAgent(agent) && !['idle', 'offline'].includes(agent.state)
}

function moveTo(agentId: string, pos: { x: number; y: number }) {
  const cur = agentPositions[agentId]
  if (cur && cur.x === pos.x && cur.y === pos.y) return
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
      const tw = 200 * station.scale
      // Stand at the front-centre of the table: ~40% across, ~55% down the image
      // (the table surface sits at ~35% from top; agent stands just in front of it)
      moveTo(agent.id, {
        x: station.x + Math.round(tw * 0.40),
        y: station.y + Math.round(tw * 0.55),
      })
    } else if (!agentPositions[agent.id]) {
      agentPositions[agent.id] = randomPos()
    }
  })
}

let walkIdx = 0
let walkTimer: ReturnType<typeof setInterval> | null = null

function tickWalk() {
  const idle = agents.value.filter((a) => !isAgentAtWork(a))
  if (idle.length === 0) return
  const agent = idle[walkIdx % idle.length]
  walkIdx++
  moveTo(agent.id, randomPos())
}

onMounted(() => {
  if (floorEl.value) {
    containerWidth.value = floorEl.value.clientWidth
    containerHeight.value = floorEl.value.clientHeight
    _ro = new ResizeObserver(([entry]) => {
      containerWidth.value = entry.contentRect.width
      containerHeight.value = entry.contentRect.height
    })
    _ro.observe(floorEl.value)
  }
  syncPositions()
  walkTimer = setInterval(tickWalk, 2000)
})

onUnmounted(() => {
  if (walkTimer) clearInterval(walkTimer)
  Object.values(walkTimers).forEach(clearTimeout)
  _ro?.disconnect()
})

watch(agents, syncPositions, { deep: true })
watch(visibleStations, syncPositions)
watch(containerWidth, syncPositions)
watch(containerHeight, syncPositions)

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
  background: url('../assets/workshop-bg.jpg') center top / cover no-repeat;
  position: relative;
  overflow: hidden;
  font-family: var(--font-pixel);
}

/* ── Sparkles ─────────────────────────────────────────────── */
.sparkle-field {
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 1;
}
.sparkle {
  position: absolute;
  animation: sparkleFade 2.5s infinite ease-in-out;
}
.sparkle:nth-child(5n + 1) { color: rgba(255, 240, 180, 0.7); }
.sparkle:nth-child(5n + 2) { color: rgba(200, 240, 220, 0.6); }
.sparkle:nth-child(5n + 3) { color: rgba(255, 200, 100, 0.65); }
.sparkle:nth-child(5n + 4) { color: rgba(255, 255, 200, 0.55); }
.sparkle:nth-child(5n)     { color: rgba(180, 230, 255, 0.6); }
@keyframes sparkleFade {
  0%, 100% { opacity: 0; transform: scale(0.3) rotate(0deg); }
  40%, 60%  { opacity: 0.9; transform: scale(1.1) rotate(180deg); }
}

/* ── Steam vents ──────────────────────────────────────────── */
.steam-vent {
  position: absolute;
  width: 16px;
  z-index: 3;
  pointer-events: none;
}
/* Positioned to match the steaming apparatus along the left/right walls */
.vent1 { left: 13%;  top: 42%; }
.vent2 { right: 12%; top: 42%; }

.steam-particle {
  position: absolute;
  width: 10px;
  height: 10px;
  background: radial-gradient(circle, rgba(255, 240, 200, 0.80) 0%, transparent 70%);
  border-radius: 50%;
  animation: steamRise 2.2s var(--delay, 0s) infinite ease-out;
}
@keyframes steamRise {
  0%   { transform: translateY(0)   scale(0.5);                       opacity: 0.75; }
  50%  { transform: translateY(-35px) scale(1.6) translateX(6px);      opacity: 0.40; }
  100% { transform: translateY(-70px) scale(2.5) translateX(-6px);     opacity: 0; }
}

/* ── Work stations ────────────────────────────────────────── */
.work-station {
  position: absolute;
  width: calc(var(--scale, 1) * 200px);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 3px;
  z-index: 4;
}
.station-desk {
  width: 100%;
  position: relative;
  /* Drop shadow so table reads against the background */
  filter: drop-shadow(0 4px 8px rgba(0, 0, 0, 0.55));
}
.work-station.occupied .station-desk {
  filter: drop-shadow(0 4px 10px rgba(0, 0, 0, 0.65)) drop-shadow(0 0 6px rgba(255, 200, 80, 0.25));
}
.station-table-img {
  width: 100%;
  display: block;
  image-rendering: pixelated;
  image-rendering: crisp-edges;
}

.station-label {
  font-size: calc(var(--scale, 1) * 6px);
  color: var(--color-brass);
  letter-spacing: 1px;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-shadow: 0 1px 3px rgba(0,0,0,0.8);
}

.task-badge {
  font-size: calc(var(--scale, 1) * 5px);
  padding: 1px 4px;
  border-radius: 2px;
  letter-spacing: 1px;
  text-transform: uppercase;
}
.state-working        { color: var(--color-green);  text-shadow: 0 0 4px var(--color-green); }
.state-pending        { color: var(--color-text-dim); }
.state-planning       { color: var(--color-sky);    text-shadow: 0 0 4px var(--color-sky); }
.state-awaiting-review { color: var(--color-amber); text-shadow: 0 0 4px var(--color-amber); }
.state-followup       { color: var(--color-orange); text-shadow: 0 0 4px var(--color-orange); }
.state-failed         { color: var(--color-red);    text-shadow: 0 0 4px var(--color-red); }
.state-done           { color: var(--color-teal);   text-shadow: 0 0 4px var(--color-teal); }
.state-cancelled      { color: var(--color-red);    text-shadow: 0 0 4px var(--color-red); }

/* ── Agents ───────────────────────────────────────────────── */
.floating-agent {
  position: absolute;
  z-index: 5;
  transition: left 2.4s ease-in-out, top 2.4s ease-in-out;
  display: flex;
  flex-direction: column;
  align-items: center;
  pointer-events: none;
}
.agent-nametag {
  font-size: 5px;
  color: var(--color-cream);
  margin-top: 2px;
  white-space: nowrap;
  text-shadow: 0 0 4px rgba(0,0,0,0.9), 0 1px 2px rgba(0,0,0,1);
  letter-spacing: 1px;
}

/* ── Info overlay ─────────────────────────────────────────── */
.factory-info {
  position: absolute;
  top: 8px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  gap: 20px;
  align-items: center;
  background: rgba(12, 6, 0, 0.75);
  border: 1px solid var(--color-brass);
  padding: 4px 14px;
  z-index: 10;
  box-shadow: 0 0 12px rgba(232, 170, 0, 0.3), inset 0 0 6px rgba(232, 170, 0, 0.06);
}
.factory-title {
  font-size: 7px;
  letter-spacing: 2px;
  background: linear-gradient(
    90deg,
    var(--color-amber), var(--color-gold), var(--color-cream),
    var(--color-teal), var(--color-gold), var(--color-amber)
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
  to   { background-position: 250% center; }
}
.agent-count {
  font-size: 7px;
  color: var(--color-teal);
  text-shadow: 0 0 6px var(--color-teal);
}

/* ── Ticker tape ──────────────────────────────────────────── */
.ticker-tape {
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  height: 28px;
  background: rgba(8, 4, 0, 0.82);
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
.ticker-msg:nth-child(3n + 2) { color: var(--color-teal);  text-shadow: 0 0 4px rgba(0, 187, 170, 0.5); }
.ticker-msg:nth-child(3n)     { color: var(--color-gold);  text-shadow: 0 0 4px rgba(255, 214, 68, 0.5); }
@keyframes tickerScroll {
  from { transform: translateX(100vw); }
  to   { transform: translateX(-100%); }
}

/* ── Mobile ───────────────────────────────────────────────── */
@media (max-width: 599px) {
  .factory-info { padding: 3px 8px; gap: 8px; }
  .factory-title { font-size: 5px; letter-spacing: 1px; }
}
</style>
