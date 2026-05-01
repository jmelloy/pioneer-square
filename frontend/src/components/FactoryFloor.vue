<template>
  <div class="factory-floor" ref="floorEl">
    <!-- Background grid -->
    <div class="floor-grid"></div>

    <!-- Warm floating sparkles -->
    <div class="sparkle-field">
      <div
        class="sparkle"
        v-for="n in 14"
        :key="`sp${n}`"
        :style="`left: ${(n * 73 + 11) % 93}%; top: ${(n * 59 + 17) % 80}%; font-size: ${8 + (n % 3) * 3}px; animation-delay: ${((n * 0.37) % 2.8).toFixed(1)}s; animation-duration: ${2.5 + (n % 3) * 0.8}s;`"
      >{{ ['✦','★','✧','⋆','✩'][n % 5] }}</div>
    </div>

    <!-- Ceiling pipes -->
    <div class="ceiling-pipe pipe-h pipe1"></div>
    <div class="ceiling-pipe pipe-h pipe2"></div>
    <div class="ceiling-pipe pipe-v pipe3"></div>
    <div class="ceiling-pipe pipe-v pipe4"></div>

    <!-- Pipe joints -->
    <div class="pipe-joint j1"></div>
    <div class="pipe-joint j2"></div>

    <!-- Steam effects -->
    <div class="steam-vent vent1">
      <div class="steam-particle" v-for="n in 4" :key="n" :style="`--delay: ${n * 0.3}s`"></div>
    </div>
    <div class="steam-vent vent2">
      <div class="steam-particle" v-for="n in 4" :key="n" :style="`--delay: ${n * 0.4}s`"></div>
    </div>
    <div class="steam-vent vent3">
      <div class="steam-particle" v-for="n in 3" :key="n" :style="`--delay: ${n * 0.5}s`"></div>
    </div>

    <!-- Gears -->
    <div class="gear gear-large g1">⚙</div>
    <div class="gear gear-medium g2">⚙</div>
    <div class="gear gear-small g3">⚙</div>
    <div class="gear gear-medium g4">⚙</div>
    <div class="gear gear-small g5">⚙</div>

    <!-- Furnace -->
    <div class="furnace">
      <div class="furnace-body">
        <div class="furnace-door">
          <div class="furnace-fire">🔥</div>
        </div>
        <div class="furnace-gauge">
          <div class="gauge-needle"></div>
        </div>
      </div>
      <div class="furnace-chimney">
        <div class="smoke-particle" v-for="n in 5" :key="n" :style="`--delay: ${n * 0.6}s`"></div>
      </div>
      <div class="furnace-label">FURNACE</div>
    </div>

    <!-- Conveyor belt -->
    <div class="conveyor-belt">
      <div class="belt-track">
        <div class="belt-item" v-for="(item, n) in beltItems" :key="n" :style="`--offset: ${n * 60}px`">{{ item }}</div>
      </div>
      <div class="belt-roller left"></div>
      <div class="belt-roller right"></div>
    </div>

    <!-- Work stations — one per active task -->
    <div
      v-for="(station, i) in visibleStations"
      :key="i"
      class="work-station"
      :style="`left: ${station.x}px; top: ${station.y}px`"
      :class="{ occupied: station.task }"
    >
      <div class="station-desk">
        <div class="station-monitor">
          <div class="monitor-screen">
            <div v-if="station.task" class="screen-active">
              <div class="screen-line" v-for="l in 3" :key="l"></div>
            </div>
            <div v-else class="screen-idle">--</div>
          </div>
        </div>
        <div class="station-table"></div>
      </div>
      <div class="station-label">{{ station.task ? truncate(station.task.name, 10) : `WS-${i + 1}` }}</div>
      <div v-if="station.task" class="task-badge" :class="`state-${station.task.state}`">
        {{ stateLabel(station.task.state) }}
      </div>
    </div>

    <!-- Points of interest -->
    <div class="poi tool-cabinet">
      <div class="tc-body">
        <div class="tc-drawer" v-for="n in 3" :key="n"></div>
      </div>
      <div class="poi-label">TOOLS</div>
    </div>

    <div class="poi bulletin-board">
      <div class="bb-frame">
        <div class="bb-pin" v-for="n in 3" :key="n" :style="`left:${8+n*18}px;top:6px`"></div>
        <div class="bb-note n1"></div>
        <div class="bb-note n2"></div>
        <div class="bb-note n3"></div>
      </div>
      <div class="poi-label">BOARD</div>
    </div>

    <div class="poi water-cooler">
      <div class="wc-bottle"></div>
      <div class="wc-base">
        <div class="wc-tap"></div>
      </div>
      <div class="wc-drop" v-for="n in 2" :key="n" :style="`--delay:${n*0.6}s`"></div>
      <div class="poi-label">H₂O</div>
    </div>

    <div class="poi coffee-maker" :style="`left: ${coffeePotPos.x}px; top: ${coffeePotPos.y}px`">
      <div class="cm-body">
        <div class="cm-tank"></div>
        <div class="cm-spout"></div>
        <div class="cm-cup">☕</div>
      </div>
      <div class="steam-particle" v-for="n in 2" :key="n" :style="`--delay:${n*0.45}s`"></div>
      <div class="poi-label">COFFEE</div>
    </div>

    <!-- Floating agents that walk to their task station or wander when idle -->
    <div
      v-for="agent in agents"
      :key="agent.id"
      class="floating-agent"
      :style="`left: ${agentPos(agent.id).x}px; top: ${agentPos(agent.id).y}px; --walk-dur: ${agentDuration[agent.id] || 1.5}s`"
    >
      <div v-if="agentChatting[agent.id]" class="chat-bubble">💬</div>
      <AgentAvatar :agent="agent" :walking="agentWalking[agent.id]" />
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
        <span v-for="(msg, i) in tickerMessages" :key="i" class="ticker-msg">
          ⚙ {{ msg }}
        </span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch, onMounted, onUnmounted } from 'vue'
import { useAgentsStore } from '../stores/agents'
import { useTasksStore } from '../stores/tasks'
import type { Agent, Task } from '../types'
import AgentAvatar from './AgentAvatar.vue'

interface Position { x: number; y: number }
interface Station extends Position { task: Task | null }

const agentsStore = useAgentsStore()
const tasksStore = useTasksStore()
const agents = computed(() => agentsStore.agents.filter(a => a.state !== 'offline'))

const beltItems = ['🔩', '⚙️', '🔧', '🪙', '⭐', '🔨']

// ── Canvas dimensions ──────────────────────────────────────
const floorEl = ref<HTMLElement | null>(null)
const floorW  = ref(800)
const floorH  = ref(600)

function updateFloorSize() {
  if (floorEl.value) {
    floorW.value = floorEl.value.clientWidth  || 800
    floorH.value = floorEl.value.clientHeight || 600
  }
}

// ── Desk layout ────────────────────────────────────────────
// 3 columns × 2 rows spread proportionally across the usable canvas.
// Usable width excludes the foreman chat pane (fixed 360px on the right)
// so desks stay clear of its boundary.
// Fixed per-desk jitter (seeded values) gives an organic, stable look.
const CHAT_PANE_W   = 360
const STATION_W     = 100
const COL_FRACS     = [0.08, 0.42, 0.78]
const ROW_FRACS     = [0.22, 0.60]
const JITTER        = [[8,-5],[-6,12],[14,-8],[-10,7],[5,10],[-12,-6]]
const GAP_X_FRACS   = [0.01, 0.25, 0.55, 0.87]

const usableW = computed(() =>
  Math.max(floorW.value - CHAT_PANE_W - STATION_W, 300)
)

const stationPositions = computed(() => {
  const W = usableW.value, H = floorH.value
  return ROW_FRACS.flatMap((yf, ri) =>
    COL_FRACS.map((xf, ci) => ({
      x: Math.round(xf * W) + JITTER[ri * 3 + ci][0],
      y: Math.round(yf * H) + JITTER[ri * 3 + ci][1],
    }))
  )
})

// ── Walkable region — full canvas minus small margins ──────
const WALK_AREA = computed(() => ({
  xMin: 15,
  xMax: usableW.value + STATION_W - 15,
  yMin: 72,
  yMax: floorH.value - 30, // leave room for ticker tape (28px)
}))

// Horizontal desk bands that agents must route around
const ROW1 = computed(() => ({
  yMin: Math.round(ROW_FRACS[0] * floorH.value) - 18,
  yMax: Math.round(ROW_FRACS[0] * floorH.value) + 82,
}))
const ROW2 = computed(() => ({
  yMin: Math.round(ROW_FRACS[1] * floorH.value) - 18,
  yMax: Math.round(ROW_FRACS[1] * floorH.value) + 82,
}))

// Clear vertical lanes between desk columns
const GAP_XS = computed(() =>
  GAP_X_FRACS.map(f => Math.round(f * usableW.value))
)

// Coffee maker sits beside the rightmost top-row desk (index 2)
const coffeePotPos = computed(() => {
  const s = stationPositions.value[2]
  return { x: s.x + 108, y: s.y - 5 }
})

// Points of interest — idle robots walk here occasionally
const POIS = computed(() => [
  { id: 'toolbox', x: 26,  y: 88 },
  { id: 'board',   x: 318, y: 88 },
  { id: 'cooler',  x: 26,  y: Math.round(floorH.value * 0.42) },
  { id: 'coffee',  x: coffeePotPos.value.x + 5, y: coffeePotPos.value.y + 30 },
])

const visibleStations = computed<Station[]>(() => {
  const active = tasksStore.tasks
    .filter(t => !['done', 'failed'].includes(t.state) && t.worker_id && t.worker_id !== 'foreman')
    .slice(0, 6)
  return stationPositions.value.map((pos, i) => ({
    ...pos,
    task: active[i] || null,
  }))
})

// Reactive per-agent state (bound to template)
const agentPositions = reactive<Record<string, Position>>({})
const agentWalking   = reactive<Record<string, boolean>>({})
const agentDuration  = reactive<Record<string, number>>({}) // CSS transition seconds
const agentChatting  = reactive<Record<string, boolean>>({})

// Non-reactive walk queues and timers
const agentWaypoints: Record<string, Position[]> = {}
const agentTimers: Record<string, ReturnType<typeof setTimeout>> = {}
const agentDestPoi: Record<string, string | null> = {}
const prevAtWork: Record<string, boolean> = {}

const SOCIAL_POIS = new Set(['cooler', 'coffee'])

function agentPos(id: string): Position {
  return agentPositions[id] || { x: Math.round(floorW.value / 2), y: Math.round(floorH.value / 2) }
}

// ── Pathfinding helpers ────────────────────────────────────

function getYZone(y: number) {
  const r1 = ROW1.value, r2 = ROW2.value
  if (y < r1.yMin) return 'top'
  if (y < r1.yMax) return 'row1'
  if (y < r2.yMin) return 'mid'
  if (y < r2.yMax) return 'row2'
  return 'bot'
}

function closestGapX(x: number) {
  return GAP_XS.value.reduce((best, gx) => Math.abs(gx - x) < Math.abs(best - x) ? gx : best)
}

// Returns a list of {x,y} waypoints from (x1,y1) to (x2,y2) that avoid station rows.
// Within the same clear zone the path is direct (one segment).
// Cross-zone paths route through the nearest column gap: H → V → H (three straight segments max).
function computeWaypoints(x1: number, y1: number, x2: number, y2: number): Position[] {
  const sz = getYZone(y1)
  const ez = getYZone(y2)

  if (sz === ez && sz !== 'row1' && sz !== 'row2') {
    return [{ x: x2, y: y2 }]
  }

  const gx = closestGapX((x1 + x2) / 2)
  const pts: Position[] = []
  if (Math.abs(x1 - gx) > 12) pts.push({ x: gx, y: y1 })
  if (Math.abs(y1 - y2) > 12) pts.push({ x: gx, y: y2 })
  pts.push({ x: x2, y: y2 })
  return pts
}

function randomInClearZone(): Position {
  const { xMin, xMax, yMin: walkYMin, yMax: walkYMax } = WALK_AREA.value
  const { yMin: r1Min, yMax: r1Max } = ROW1.value
  const { yMin: r2Min, yMax: r2Max } = ROW2.value
  const zones = [
    { yMin: walkYMin,  yMax: r1Min - 2 },
    { yMin: r1Max + 2, yMax: r2Min - 2 },
    { yMin: r2Max + 2, yMax: walkYMax  },
  ].filter(z => z.yMax > z.yMin + 10)
  if (zones.length === 0) return { x: (xMin + xMax) / 2, y: (walkYMin + walkYMax) / 2 }
  const z = zones[Math.floor(Math.random() * zones.length)]
  return {
    x: xMin + Math.random() * (xMax - xMin),
    y: z.yMin + Math.random() * (z.yMax - z.yMin),
  }
}

function pickDestination(): { pos: Position; poiId: string | null } {
  const pois = POIS.value
  if (Math.random() < 0.55) {
    const p = pois[Math.floor(Math.random() * pois.length)]
    return { pos: { x: p.x, y: p.y }, poiId: p.id }
  }
  return { pos: randomInClearZone(), poiId: null }
}

function walkDuration(x1: number, y1: number, x2: number, y2: number) {
  const dist = Math.hypot(x2 - x1, y2 - y1)
  return Math.max(0.65, dist / 130) // 130 px/s, min 0.65 s
}

// ── Walk step machine ──────────────────────────────────────

function tryStartChat(id: string): boolean {
  const myPoi = agentDestPoi[id]
  if (!myPoi || !SOCIAL_POIS.has(myPoi)) return false
  const partner = agents.value.find(a =>
    a.id !== id &&
    agentDestPoi[a.id] === myPoi &&
    !agentWalking[a.id] &&
    !agentChatting[a.id] &&
    !isAgentAtWork(a)
  )
  if (!partner) return false
  const dur = 10000 + Math.random() * 5000 // 10–15s
  agentChatting[id] = true
  agentChatting[partner.id] = true
  clearTimeout(agentTimers[partner.id])
  agentTimers[id] = setTimeout(() => {
    agentChatting[id] = false
    scheduleWalk(id)
  }, dur)
  agentTimers[partner.id] = setTimeout(() => {
    agentChatting[partner.id] = false
    scheduleWalk(partner.id)
  }, dur + 250)
  return true
}

function stepAgent(id: string) {
  const queue = agentWaypoints[id]
  if (!queue || queue.length === 0) {
    agentWalking[id] = false
    if (tryStartChat(id)) return
    const rest = 4000 + Math.random() * 5000 // 4–9s general lingering
    agentTimers[id] = setTimeout(() => scheduleWalk(id), rest)
    return
  }
  const cur = agentPositions[id] || { x: Math.round(floorW.value / 2), y: Math.round(floorH.value / 2) }
  const next = queue.shift()!
  const dur = walkDuration(cur.x, cur.y, next.x, next.y)
  agentDuration[id] = dur
  agentWalking[id] = true
  agentPositions[id] = next
  agentTimers[id] = setTimeout(() => stepAgent(id), dur * 1000 + 50)
}

function scheduleWalk(id: string) {
  const agent = agents.value.find(a => a.id === id)
  if (!agent || isAgentAtWork(agent)) return
  agentChatting[id] = false
  const cur = agentPositions[id] || randomInClearZone()
  const dest = pickDestination()
  agentDestPoi[id] = dest.poiId
  agentWaypoints[id] = computeWaypoints(cur.x, cur.y, dest.pos.x, dest.pos.y)
  stepAgent(id)
}

// ── Agent / station helpers ────────────────────────────────

function stationForAgent(agent: Agent) {
  if (!agent.workerId) return null
  return visibleStations.value.find(s => s.task && s.task.worker_id === agent.workerId) || null
}

function isAgentAtWork(agent: Agent) {
  return !!stationForAgent(agent) && !['idle', 'offline'].includes(agent.state)
}

function sendAgentToStation(agent: Agent) {
  const station = stationForAgent(agent)
  if (!station) return
  const target = { x: station.x + 25, y: station.y + 90 }
  const cur = agentPositions[agent.id] || randomInClearZone()
  clearTimeout(agentTimers[agent.id])
  agentChatting[agent.id] = false
  agentDestPoi[agent.id] = null
  agentWaypoints[agent.id] = computeWaypoints(cur.x, cur.y, target.x, target.y)
  stepAgent(agent.id)
}

function initAgent(agent: Agent) {
  if (!agentPositions[agent.id]) {
    agentPositions[agent.id] = randomInClearZone()
    agentDuration[agent.id] = 1.5
  }
  if (isAgentAtWork(agent)) {
    sendAgentToStation(agent)
  } else {
    const delay = 600 + Math.random() * 2000
    agentTimers[agent.id] = setTimeout(() => scheduleWalk(agent.id), delay)
  }
  prevAtWork[agent.id] = isAgentAtWork(agent)
}

function syncPositions() {
  agents.value.forEach(agent => {
    const atWork = isAgentAtWork(agent)
    const wasAtWork = prevAtWork[agent.id]

    if (!agentPositions[agent.id]) {
      initAgent(agent)
      return
    }

    if (atWork && !wasAtWork) {
      sendAgentToStation(agent)
    } else if (!atWork && wasAtWork) {
      clearTimeout(agentTimers[agent.id])
      const delay = 400 + Math.random() * 1200
      agentTimers[agent.id] = setTimeout(() => scheduleWalk(agent.id), delay)
    }
    prevAtWork[agent.id] = atWork
  })
}

onMounted(() => {
  updateFloorSize()
  window.addEventListener('resize', updateFloorSize)
  agents.value.forEach(initAgent)
})

onUnmounted(() => {
  window.removeEventListener('resize', updateFloorSize)
  Object.values(agentTimers).forEach(clearTimeout)
})

// Watch agent id+state+workerId as a flat string so every agent's state change
// triggers syncPositions independently, regardless of array-reference stability.
watch(
  () => agents.value.map(a => `${a.id}:${a.state}:${a.workerId}`).join(','),
  syncPositions
)
watch(visibleStations, syncPositions)

const tickerMessages = computed(() => {
  const msgs = []
  agents.value.forEach(a => msgs.push(`${a.name}: ${a.state.toUpperCase()}`))
  tasksStore.tasks
    .filter(t => !['done', 'failed'].includes(t.state))
    .slice(0, 4)
    .forEach(t => msgs.push(`TASK: ${t.name}`))
  if (msgs.length === 0) msgs.push('AWAITING WORKERS', 'SYSTEMS NOMINAL', 'BOILER PRESSURE: 87 PSI')
  return msgs
})

function truncate(str?: string, len = 10) {
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
  background: linear-gradient(180deg, #0d0600 0%, #120900 40%, #1c1000 100%);
  position: relative;
  overflow: hidden;
  font-family: var(--font-pixel);
}

/* Floor grid — warm brass tint */
.floor-grid {
  position: absolute;
  inset: 0;
  background-image:
    linear-gradient(rgba(232, 170, 0, 0.06) 1px, transparent 1px),
    linear-gradient(90deg, rgba(232, 170, 0, 0.06) 1px, transparent 1px);
  background-size: 40px 40px;
}

/* Sparkle field — warm star colors */
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

.sparkle:nth-child(5n+1) { color: var(--color-gold); }
.sparkle:nth-child(5n+2) { color: var(--color-teal); }
.sparkle:nth-child(5n+3) { color: var(--color-amber); }
.sparkle:nth-child(5n+4) { color: var(--color-cream); }
.sparkle:nth-child(5n)   { color: var(--color-orange); }

@keyframes sparkleFade {
  0%, 100% { opacity: 0; transform: scale(0.3) rotate(0deg); }
  40%, 60%  { opacity: 0.85; transform: scale(1.1) rotate(180deg); }
}

/* Pipes — rich warm copper */
.ceiling-pipe {
  position: absolute;
  background: linear-gradient(180deg, #7a3c00 0%, #cc5500 35%, #ee7722 55%, #8a4400 100%);
  border: 1px solid #7a3c00;
  box-shadow: 0 0 6px rgba(204, 85, 0, 0.4);
}

.pipe-h { height: 12px; }
.pipe-v { width: 12px; }

.pipe1 { top: 20px; left: 0; right: 0; }
.pipe2 { top: 50px; left: 100px; width: 200px; }
.pipe3 { top: 0; left: 150px; height: 80px; }
.pipe4 { top: 0; left: 400px; height: 60px; }

/* Pipe joints (bolted flanges) */
.pipe-joint {
  position: absolute;
  width: 18px;
  height: 18px;
  background: radial-gradient(circle, var(--color-brass-light) 20%, var(--color-brass) 60%, var(--color-brass-dark) 100%);
  border: 2px solid var(--color-brass-dark);
  border-radius: 50%;
  box-shadow: 0 0 5px rgba(232, 170, 0, 0.5);
}

.j1 { top: 14px; left: 147px; }
.j2 { top: 14px; left: 397px; }

/* Steam vents */
.steam-vent {
  position: absolute;
  width: 14px;
}

.vent1 { top: 32px; left: 155px; }
.vent2 { top: 32px; left: 405px; }
.vent3 { top: 62px; left: 230px; }

.steam-particle {
  position: absolute;
  width: 8px;
  height: 8px;
  background: radial-gradient(circle, rgba(255, 232, 176, 0.85) 0%, transparent 70%);
  border-radius: 50%;
  animation: steamRise 2s var(--delay, 0s) infinite ease-out;
}

@keyframes steamRise {
  0%   { transform: translateY(0) scale(0.5); opacity: 0.8; }
  50%  { transform: translateY(-30px) scale(1.5) translateX(5px); opacity: 0.4; }
  100% { transform: translateY(-60px) scale(2) translateX(-5px); opacity: 0; }
}

/* Gears — warm SNES palette, each a distinct color */
.gear {
  position: absolute;
  user-select: none;
  line-height: 1;
}

.gear-large  { font-size: 56px; animation: gearSpin 8s linear infinite; }
.gear-medium { font-size: 36px; animation: gearSpin 5s linear infinite reverse; }
.gear-small  { font-size: 22px; animation: gearSpin 3s linear infinite; }

.g1 { right: 30px;  top: 60px;  color: var(--color-gold);    text-shadow: 0 0 10px var(--color-gold),   0 0 20px rgba(255,214,68,0.4); }
.g2 { right: 75px;  top: 80px;  color: var(--color-teal);    text-shadow: 0 0 10px var(--color-teal),   0 0 20px rgba(0,187,170,0.4); }
.g3 { right: 55px;  top: 100px; color: var(--color-orange);  text-shadow: 0 0 10px var(--color-orange), 0 0 20px rgba(255,119,0,0.4); }
.g4 { left: 620px;  top: 40px;  color: var(--color-sky);     text-shadow: 0 0 10px var(--color-sky),    0 0 20px rgba(68,170,238,0.4); }
.g5 { left: 650px;  top: 65px;  color: var(--color-amber);   text-shadow: 0 0 10px var(--color-amber),  0 0 20px rgba(255,204,0,0.4); }

@keyframes gearSpin {
  from { transform: rotate(0deg); }
  to   { transform: rotate(360deg); }
}

/* Furnace */
.furnace {
  position: absolute;
  right: 120px;
  top: 60px;
}

.furnace-body {
  width: 60px;
  height: 80px;
  background: linear-gradient(180deg, #2a1200 0%, #180900 100%);
  border: 3px solid var(--color-copper);
  border-radius: 4px 4px 0 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: space-around;
  padding: 6px;
  box-shadow: 0 0 14px rgba(204, 85, 0, 0.4), inset 0 0 8px rgba(255, 119, 0, 0.1);
}

.furnace-door {
  width: 36px;
  height: 36px;
  background: #0d0400;
  border: 2px solid var(--color-copper-light);
  border-radius: 2px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 22px;
  animation: flicker 0.3s infinite alternate;
  box-shadow: inset 0 0 10px rgba(255, 100, 0, 0.5);
}

@keyframes flicker {
  from { opacity: 0.85; box-shadow: inset 0 0 8px rgba(255, 80, 0, 0.4); }
  to   { opacity: 1;    box-shadow: inset 0 0 14px rgba(255, 120, 0, 0.7); }
}

.furnace-gauge {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: #0d0400;
  border: 2px solid var(--color-brass);
  position: relative;
  overflow: hidden;
  box-shadow: 0 0 4px var(--color-brass-dark);
}

.gauge-needle {
  position: absolute;
  left: 50%;
  bottom: 50%;
  width: 2px;
  height: 8px;
  background: var(--color-red);
  transform-origin: bottom center;
  animation: needleSpin 3s ease-in-out infinite alternate;
  box-shadow: 0 0 4px var(--color-red);
}

@keyframes needleSpin {
  from { transform: rotate(-60deg); }
  to   { transform: rotate(60deg); }
}

.furnace-chimney {
  position: absolute;
  top: -30px;
  left: 50%;
  transform: translateX(-50%);
  width: 16px;
  height: 30px;
  background: linear-gradient(180deg, #2a1200 0%, #180900 100%);
  border: 2px solid var(--color-copper);
  border-bottom: none;
}

.smoke-particle {
  position: absolute;
  top: 0;
  left: 50%;
  width: 10px;
  height: 10px;
  background: radial-gradient(circle, rgba(160, 100, 40, 0.7) 0%, transparent 70%);
  border-radius: 50%;
  animation: smokeRise 3s var(--delay, 0s) infinite ease-out;
}

@keyframes smokeRise {
  0%   { transform: translate(-50%, 0) scale(0.5); opacity: 0.8; }
  100% { transform: translate(calc(-50% + 20px), -50px) scale(3); opacity: 0; }
}

.furnace-label {
  position: absolute;
  bottom: -18px;
  left: 50%;
  transform: translateX(-50%);
  font-size: 5px;
  color: var(--color-copper);
  white-space: nowrap;
  letter-spacing: 1px;
  text-shadow: 0 0 4px var(--color-copper);
}

/* Conveyor belt */
.conveyor-belt {
  position: absolute;
  bottom: 80px;
  left: 60px;
  right: 200px;
  height: 28px;
}

.belt-track {
  width: 100%;
  height: 18px;
  background: repeating-linear-gradient(
    90deg,
    #2a1800 0px,
    #2a1800 18px,
    #3a2200 18px,
    #3a2200 36px
  );
  border: 2px solid var(--color-copper-light);
  position: relative;
  overflow: hidden;
}

.belt-item {
  position: absolute;
  font-size: 13px;
  top: 1px;
  animation: beltMove 4s linear infinite;
  animation-delay: calc(var(--offset, 0) * -1ms / 10);
}

.belt-item:nth-child(6n+1) { filter: drop-shadow(0 0 3px var(--color-gold)); }
.belt-item:nth-child(6n+2) { filter: drop-shadow(0 0 3px var(--color-teal)); }
.belt-item:nth-child(6n+3) { filter: drop-shadow(0 0 3px var(--color-amber)); }
.belt-item:nth-child(6n+4) { filter: drop-shadow(0 0 3px var(--color-sky)); }
.belt-item:nth-child(6n+5) { filter: drop-shadow(0 0 3px var(--color-orange)); }
.belt-item:nth-child(6n)   { filter: drop-shadow(0 0 3px var(--color-gold)); }

@keyframes beltMove {
  from { transform: translateX(120%); }
  to   { transform: translateX(-100px); }
}

.belt-roller {
  position: absolute;
  bottom: 0;
  width: 28px;
  height: 28px;
  background: radial-gradient(circle, var(--color-copper-light) 20%, var(--color-copper) 55%, var(--color-brass-dark) 100%);
  border-radius: 50%;
  border: 3px solid var(--color-brass-dark);
  box-shadow: 0 0 6px rgba(204, 85, 0, 0.5);
}

.belt-roller.left  { left: -14px; }
.belt-roller.right { right: -14px; }

/* Work Stations */
.work-station {
  position: absolute;
  width: 100px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
}

.station-desk {
  width: 100%;
  position: relative;
}

.station-monitor {
  width: 60px;
  height: 48px;
  background: #0d0600;
  border: 3px solid var(--color-brass-dark);
  border-radius: 2px;
  margin: 0 auto;
  display: flex;
  align-items: stretch;
  overflow: hidden;
}

.monitor-screen {
  flex: 1;
  background: #080400;
  border: 2px solid #1c1000;
  padding: 4px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.screen-active {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.screen-line {
  height: 3px;
  opacity: 0.85;
  animation: screenScroll 2s infinite linear;
  border-radius: 1px;
}

.screen-line:nth-child(1) { background: var(--color-teal); }
.screen-line:nth-child(2) { background: var(--color-sky); animation-delay: -0.7s; opacity: 0.5; }
.screen-line:nth-child(3) { background: var(--color-green); animation-delay: -1.4s; opacity: 0.3; width: 60%; }

@keyframes screenScroll {
  0%   { opacity: 0.85; }
  50%  { opacity: 0.3; }
  100% { opacity: 0.85; }
}

.screen-idle {
  font-family: var(--font-pixel);
  font-size: 8px;
  color: #3a2510;
}

.station-table {
  width: 100%;
  height: 14px;
  background: linear-gradient(180deg, #5a3818 0%, #3a2010 100%);
  border: 2px solid var(--color-copper-light);
  border-top: 3px solid var(--color-brass);
}

.station-agent, .station-empty {
  margin-top: 2px;
}


.empty-slot {
  width: 30px;
  height: 50px;
  background: rgba(232, 170, 0, 0.04);
  border: 1px dashed var(--color-brass-dark);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text-dim);
  font-size: 16px;
  font-family: var(--font-mono);
}

.station-label {
  font-size: 5px;
  color: var(--color-brass-dark);
  letter-spacing: 1px;
  max-width: 100px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.task-badge {
  font-size: 5px;
  padding: 1px 4px;
  border-radius: 2px;
  letter-spacing: 1px;
  text-transform: uppercase;
}

.state-working       { color: var(--color-green);  text-shadow: 0 0 4px var(--color-green); }
.state-pending       { color: var(--color-text-dim); }
.state-planning      { color: var(--color-sky);    text-shadow: 0 0 4px var(--color-sky); }
.state-awaiting-review { color: var(--color-amber); text-shadow: 0 0 4px var(--color-amber); }
.state-followup      { color: var(--color-orange); text-shadow: 0 0 4px var(--color-orange); }
.state-failed        { color: var(--color-red);    text-shadow: 0 0 4px var(--color-red); }
.state-done          { color: var(--color-teal);   text-shadow: 0 0 4px var(--color-teal); }

.work-station.occupied .station-table {
  border-top-color: var(--color-brass-light);
  background: linear-gradient(180deg, #6a4820 0%, #4a2e12 100%);
}

/* Floating agents — speed set per-agent via --walk-dur CSS var */
.floating-agent {
  position: absolute;
  z-index: 5;
  transition: left var(--walk-dur, 1.5s) ease-in-out, top var(--walk-dur, 1.5s) ease-in-out;
  display: flex;
  flex-direction: column;
  align-items: center;
  pointer-events: none;
}

.agent-nametag {
  font-size: 5px;
  color: var(--color-brass);
  margin-top: 2px;
  white-space: nowrap;
  text-shadow: 0 0 4px rgba(232, 170, 0, 0.5);
  letter-spacing: 1px;
}

.chat-bubble {
  font-size: 10px;
  line-height: 1;
  margin-bottom: 2px;
  filter: drop-shadow(0 0 3px rgba(255, 214, 68, 0.5));
  animation: chatBob 1.4s infinite ease-in-out;
}

@keyframes chatBob {
  0%, 100% { transform: translateY(0); opacity: 0.9; }
  50%       { transform: translateY(-2px); opacity: 1; }
}

/* ── Points of interest ──────────────────────────────── */
.poi {
  position: absolute;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 3px;
  z-index: 3;
}
.poi-label {
  font-size: 5px;
  color: var(--color-brass-dark);
  letter-spacing: 1px;
  white-space: nowrap;
  text-shadow: 0 0 4px rgba(232,170,0,0.4);
}

/* Tool cabinet — top-left */
.tool-cabinet {
  left: 8px;
  top: 28px;
}
.tc-body {
  width: 34px;
  background: linear-gradient(180deg, #2a1200 0%, #180900 100%);
  border: 2px solid var(--color-copper);
  border-radius: 2px;
  padding: 3px 4px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  box-shadow: 0 0 6px rgba(204,85,0,0.25);
}
.tc-drawer {
  height: 7px;
  background: linear-gradient(90deg, #3a2010, #4a2c18, #3a2010);
  border: 1px solid var(--color-copper-light);
  border-radius: 1px;
  position: relative;
}
.tc-drawer::after {
  content: '';
  position: absolute;
  left: 50%;
  top: 50%;
  transform: translate(-50%, -50%);
  width: 6px;
  height: 2px;
  background: var(--color-brass);
  border-radius: 1px;
}

/* Bulletin board — top-center */
.bulletin-board {
  left: 265px;
  top: 24px;
}
.bb-frame {
  width: 72px;
  height: 46px;
  background: #2a1a08;
  border: 3px solid var(--color-brass-dark);
  border-radius: 2px;
  position: relative;
  overflow: hidden;
  box-shadow: 0 0 8px rgba(232,170,0,0.2);
}
.bb-pin {
  position: absolute;
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--color-red);
  box-shadow: 0 0 3px var(--color-red);
}
.bb-note {
  position: absolute;
  border-radius: 1px;
}
.bb-note.n1 { width: 24px; height: 16px; background: #ffe8a0; left: 6px;  top: 14px; transform: rotate(-2deg); }
.bb-note.n2 { width: 20px; height: 14px; background: #a0f0d0; left: 34px; top: 12px; transform: rotate(3deg); }
.bb-note.n3 { width: 18px; height: 12px; background: #f0c0a0; left: 20px; top: 28px; transform: rotate(-1deg); }

/* Water cooler — mid-left */
.water-cooler {
  left: 8px;
  top: 176px;
}
.wc-bottle {
  width: 22px;
  height: 36px;
  background: linear-gradient(180deg, rgba(68,170,238,0.4) 0%, rgba(68,170,238,0.15) 100%);
  border: 2px solid var(--color-sky);
  border-radius: 4px 4px 2px 2px;
  box-shadow: 0 0 6px rgba(68,170,238,0.3);
}
.wc-base {
  width: 30px;
  height: 20px;
  background: linear-gradient(180deg, #2a1200, #180900);
  border: 2px solid var(--color-copper);
  border-radius: 2px;
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
}
.wc-tap {
  width: 8px;
  height: 5px;
  background: var(--color-brass);
  border-radius: 0 2px 2px 0;
  box-shadow: 0 0 3px var(--color-brass-dark);
}
.wc-drop {
  position: absolute;
  width: 4px;
  height: 5px;
  background: var(--color-sky);
  border-radius: 50% 50% 40% 40%;
  left: 50%;
  top: 48px;
  margin-left: -2px;
  animation: drip 1.8s var(--delay, 0s) infinite ease-in;
  opacity: 0.7;
}
@keyframes drip {
  0%   { transform: translateY(-8px); opacity: 0.8; }
  60%  { opacity: 0.6; }
  100% { transform: translateY(12px); opacity: 0; }
}

/* Coffee maker — position set dynamically beside the top-right desk */
.cm-body {
  width: 40px;
  height: 50px;
  background: linear-gradient(180deg, #1a0e00, #120900);
  border: 2px solid var(--color-copper);
  border-radius: 3px 3px 1px 1px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-end;
  padding-bottom: 4px;
  position: relative;
  box-shadow: 0 0 8px rgba(204,85,0,0.3);
}
.cm-tank {
  position: absolute;
  top: 4px;
  left: 6px;
  right: 6px;
  height: 20px;
  background: linear-gradient(180deg, rgba(68,170,238,0.25), rgba(68,170,238,0.08));
  border: 1px solid var(--color-sky);
  border-radius: 2px;
}
.cm-spout {
  position: absolute;
  bottom: 12px;
  right: -8px;
  width: 10px;
  height: 3px;
  background: var(--color-copper);
  border-radius: 0 2px 2px 0;
}
.cm-cup {
  font-size: 14px;
  margin-bottom: -2px;
}

/* Factory info overlay */
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
  box-shadow: 0 0 12px rgba(232, 170, 0, 0.3), inset 0 0 6px rgba(232, 170, 0, 0.06);
}

.factory-title {
  font-size: 7px;
  letter-spacing: 2px;
  background: linear-gradient(90deg,
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
  to   { background-position: 250% center; }
}

.agent-count {
  font-size: 7px;
  color: var(--color-teal);
  text-shadow: 0 0 6px var(--color-teal);
}

/* Ticker tape */
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

.ticker-msg:nth-child(3n+1) { color: var(--color-amber); }
.ticker-msg:nth-child(3n+2) { color: var(--color-teal); text-shadow: 0 0 4px rgba(0,187,170,0.5); }
.ticker-msg:nth-child(3n)   { color: var(--color-gold); text-shadow: 0 0 4px rgba(255,214,68,0.5); }

@keyframes tickerScroll {
  from { transform: translateX(100vw); }
  to   { transform: translateX(-100%); }
}
</style>
