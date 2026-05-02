<template>
  <div class="factory-floor" ref="floorEl">
    <!-- Background atmosphere — pared down -->
    <div class="floor-grid"></div>

    <div class="sparkle-field">
      <div
        class="sparkle"
        v-for="n in 6"
        :key="`sp${n}`"
        :style="`left: ${(n * 79 + 11) % 92}%; top: ${(n * 53 + 9) % 88}%; font-size: ${9 + (n % 2) * 3}px; animation-delay: ${((n * 0.41) % 2.6).toFixed(1)}s; animation-duration: ${3 + (n % 3) * 0.6}s;`"
      >{{ ['✦','★','✧','⋆'][n % 4] }}</div>
    </div>

    <div class="ceiling-pipe"></div>
    <div class="pipe-joint j1"></div>
    <div class="pipe-joint j2"></div>

    <div class="steam-vent vent1">
      <div class="steam-particle" v-for="n in 3" :key="n" :style="`--delay: ${n * 0.4}s`"></div>
    </div>
    <div class="steam-vent vent2">
      <div class="steam-particle" v-for="n in 3" :key="n" :style="`--delay: ${n * 0.5}s`"></div>
    </div>

    <div class="ambient-gear g1">⚙</div>
    <div class="ambient-gear g2">⚙</div>

    <!-- Header -->
    <div class="factory-info">
      <span class="factory-title">⚙ PIONEER SQUARE WORKSHOP ⚙</span>
      <span class="agent-count">Agents: {{ agents.length }}</span>
    </div>

    <!-- Tasks table — each row is a workbench -->
    <div class="task-table" :style="`top: ${TABLE_TOP}px; left: ${TABLE_LEFT}px; width: ${tableWidth}px;`">
      <div
        v-for="row in taskRows"
        :key="row.task ? row.task.id : `empty-${row.index}`"
        class="task-row"
        :class="{ occupied: !!row.task, [`state-${row.task?.state || 'empty'}`]: true }"
        :style="`height: ${ROW_H}px;`"
      >
        <div class="row-header">
          <div class="row-num">{{ String(row.index + 1).padStart(2, '0') }}</div>
          <div class="row-title">{{ row.task ? truncate(row.task.name || row.task.id, 28) : '— vacant bench —' }}</div>
          <div v-if="row.task" class="row-state" :class="`state-${row.task.state}`">
            {{ stateLabel(row.task.state) }}
          </div>
        </div>

        <div class="row-bench">
          <div class="bench-rail"></div>
          <div class="bench-bolts">
            <span class="bolt" v-for="n in 6" :key="n"></span>
          </div>

          <div
            v-for="st in STATIONS"
            :key="st.key"
            class="bench-station"
            :class="[`station-${st.key}`, { active: row.activityKey === st.key }]"
            :style="`left: ${st.frac * 100}%`"
          >
            <div class="station-icon">
              <!-- Archive -->
              <template v-if="st.component === 'archive'">
                <span class="mini-book b1"></span>
                <span class="mini-book b2"></span>
                <span class="mini-book b3"></span>
                <span class="mini-book b4"></span>
              </template>
              <!-- Scrying orb -->
              <template v-else-if="st.component === 'orb'">
                <span class="mini-orb-glow"></span>
                <span class="mini-orb">🔮</span>
              </template>
              <!-- Telegraph -->
              <template v-else-if="st.component === 'telegraph'">
                <span class="mini-tg-body">
                  <span class="mini-tg-key"></span>
                  <span class="mini-tg-spark"></span>
                </span>
              </template>
              <!-- Think tank -->
              <template v-else-if="st.component === 'tank'">
                <span class="mini-tank">
                  <span class="mini-bubble" v-for="n in 2" :key="n" :style="`--delay: ${n * 0.4}s`"></span>
                </span>
              </template>
              <!-- Engine -->
              <template v-else-if="st.component === 'engine'">
                <span class="mini-engine">⚙</span>
              </template>
              <!-- Forge -->
              <template v-else-if="st.component === 'forge'">
                <span class="mini-forge">🔥</span>
              </template>
            </div>
            <div class="station-label">{{ st.label }}</div>
          </div>
        </div>
      </div>
    </div>

    <!-- Break room (idle agents lounge here) -->
    <div class="break-room" :style="`top: ${breakRoomTop}px; left: ${TABLE_LEFT}px; width: ${tableWidth}px;`">
      <div class="break-label">⏚ BREAK ROOM ⏚</div>
    </div>

    <!-- Agents — single overlay; positioned absolutely over the floor -->
    <div
      v-for="agent in agents"
      :key="agent.id"
      class="floating-agent"
      :style="`left: ${agentPos(agent.id).x}px; top: ${agentPos(agent.id).y}px; --walk-dur: ${agentDuration[agent.id] || 1.5}s;`"
    >
      <AgentAvatar :agent="agent" :walking="!!agentWalking[agent.id]" />
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
import type { Agent, AgentActivity, Task } from '../types'
import AgentAvatar from './AgentAvatar.vue'

interface Position { x: number; y: number }

const agentsStore = useAgentsStore()
const tasksStore = useTasksStore()
const agents = computed(() => agentsStore.agents.filter(a => a.state !== 'offline'))

// ── Stations on each bench ─────────────────────────────────
const STATIONS = [
  { key: 'reading',   label: 'ARCHIVE',    frac: 0.10, component: 'archive' },
  { key: 'searching', label: 'SCRYING',    frac: 0.25, component: 'orb' },
  { key: 'fetching',  label: 'TELEGRAPH',  frac: 0.40, component: 'telegraph' },
  { key: 'thinking',  label: 'THINK TANK', frac: 0.56, component: 'tank' },
  { key: 'running',   label: 'ENGINE',     frac: 0.72, component: 'engine' },
  { key: 'editing',   label: 'FORGE',      frac: 0.88, component: 'forge' },
] as const

type StationKey = typeof STATIONS[number]['key']

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
const TABLE_LEFT  = 28
const TABLE_TOP   = 56
const ROW_H       = 92
const ROW_GAP     = 8
const MAX_ROWS    = 4
const TICKER_H    = 28
const BREAK_PAD   = 20

const floorEl = ref<HTMLElement | null>(null)
const floorW  = ref(900)
const floorH  = ref(600)

function updateFloorSize() {
  if (floorEl.value) {
    floorW.value = floorEl.value.clientWidth  || 900
    floorH.value = floorEl.value.clientHeight || 600
  }
}

const tableWidth = computed(() =>
  Math.max(floorW.value - CHAT_PANE_W - TABLE_LEFT * 2, 420)
)

const visibleRowCount = computed(() => {
  const active = activeTasks.value.length
  return Math.max(2, Math.min(MAX_ROWS, active || 2))
})

const tableHeight = computed(() =>
  visibleRowCount.value * ROW_H + (visibleRowCount.value - 1) * ROW_GAP
)

const breakRoomTop = computed(() =>
  TABLE_TOP + tableHeight.value + BREAK_PAD
)

const breakRoomHeight = computed(() =>
  Math.max(60, floorH.value - breakRoomTop.value - TICKER_H - 8)
)

// ── Tasks → rows ───────────────────────────────────────────
const activeTasks = computed<Task[]>(() =>
  tasksStore.tasks.filter(t =>
    !['done', 'failed', 'cancelled'].includes(t.state) &&
    t.worker_id && t.worker_id !== 'foreman'
  ).slice(0, MAX_ROWS)
)

interface TaskRow {
  index: number
  task: Task | null
  agent: Agent | null
  activityKey: StationKey | null
}

const taskRows = computed<TaskRow[]>(() => {
  const tasks = activeTasks.value
  const rows: TaskRow[] = []
  for (let i = 0; i < visibleRowCount.value; i++) {
    const task = tasks[i] || null
    const agent = task ? agents.value.find(a => a.workerId === task.worker_id) || null : null
    const activityKey = agent && agent.activity ? ACTIVITY_TO_STATION[agent.activity] : null
    rows.push({ index: i, task, agent, activityKey })
  }
  return rows
})

function rowYFor(rowIndex: number) {
  return TABLE_TOP + rowIndex * (ROW_H + ROW_GAP)
}

// Agent stands on the bench surface (top quarter of row + small lift).
function stationPos(rowIndex: number, key: StationKey): Position {
  const station = STATIONS.find(s => s.key === key)!
  const x = TABLE_LEFT + station.frac * tableWidth.value
  const y = rowYFor(rowIndex) + ROW_H * 0.30
  return { x, y }
}

function rowCenterPos(rowIndex: number): Position {
  return {
    x: TABLE_LEFT + tableWidth.value * 0.50,
    y: rowYFor(rowIndex) + ROW_H * 0.30,
  }
}

// ── Agent movement ─────────────────────────────────────────
const agentPositions = reactive<Record<string, Position>>({})
const agentWalking   = reactive<Record<string, boolean>>({})
const agentDuration  = reactive<Record<string, number>>({})
const agentTimers: Record<string, ReturnType<typeof setTimeout>> = {}
const prevTargetKey: Record<string, string> = {}

function agentPos(id: string): Position {
  return agentPositions[id] || { x: TABLE_LEFT + 60, y: TABLE_TOP + ROW_H }
}

function rowForAgent(agent: Agent): TaskRow | null {
  if (!agent.workerId) return null
  return taskRows.value.find(r => r.task && r.task.worker_id === agent.workerId) || null
}

function isWorking(agent: Agent) {
  return !['idle', 'offline'].includes(agent.state)
}

function targetForAgent(agent: Agent): Position {
  const row = rowForAgent(agent)
  if (row && isWorking(agent)) {
    const key = row.activityKey
    return key ? stationPos(row.index, key) : rowCenterPos(row.index)
  }
  // idle: a roaming spot in the break room
  return randomBreakRoomPos(agent.id)
}

function targetKey(agent: Agent): string {
  const row = rowForAgent(agent)
  if (row && isWorking(agent)) {
    return `task:${row.task?.id ?? row.index}:${row.activityKey ?? 'center'}`
  }
  return 'idle'
}

function moveAgent(id: string, target: Position) {
  const cur = agentPositions[id] || target
  const dist = Math.hypot(target.x - cur.x, target.y - cur.y)
  const dur = Math.max(0.6, dist / 120)
  agentDuration[id] = dur
  agentWalking[id] = true
  agentPositions[id] = target
  clearTimeout(agentTimers[id])
  agentTimers[id] = setTimeout(() => {
    agentWalking[id] = false
    const agent = agents.value.find(a => a.id === id)
    if (agent && !isWorking(agent)) scheduleIdleStroll(id)
  }, dur * 1000 + 60)
}

function scheduleIdleStroll(id: string) {
  const linger = 3500 + Math.random() * 4000
  clearTimeout(agentTimers[id])
  agentTimers[id] = setTimeout(() => {
    const agent = agents.value.find(a => a.id === id)
    if (!agent || isWorking(agent)) return
    moveAgent(id, randomBreakRoomPos(id))
  }, linger)
}

function randomBreakRoomPos(_id: string): Position {
  const xMin = TABLE_LEFT + 30
  const xMax = TABLE_LEFT + tableWidth.value - 30
  const yMin = breakRoomTop.value + 24
  const yMax = breakRoomTop.value + Math.max(28, breakRoomHeight.value - 16)
  return {
    x: xMin + Math.random() * (xMax - xMin),
    y: yMin + Math.random() * Math.max(2, yMax - yMin),
  }
}

function syncAgent(agent: Agent) {
  const key = targetKey(agent)
  if (prevTargetKey[agent.id] === key && agentPositions[agent.id]) return
  prevTargetKey[agent.id] = key
  if (!agentPositions[agent.id]) {
    agentPositions[agent.id] = randomBreakRoomPos(agent.id)
  }
  moveAgent(agent.id, targetForAgent(agent))
}

function syncAll() {
  agents.value.forEach(syncAgent)
}

onMounted(() => {
  updateFloorSize()
  window.addEventListener('resize', updateFloorSize)
  syncAll()
})

onUnmounted(() => {
  window.removeEventListener('resize', updateFloorSize)
  Object.values(agentTimers).forEach(clearTimeout)
})

watch(
  () => agents.value.map(a => `${a.id}:${a.state}:${a.workerId}:${a.activity ?? ''}`).join(','),
  syncAll,
)
watch(taskRows, syncAll)
watch([floorW, floorH], syncAll)

// ── Ticker ─────────────────────────────────────────────────
const tickerMessages = computed(() => {
  const msgs: string[] = []
  agents.value.forEach(a => {
    const label = a.activity ? `${a.state.toUpperCase()} [${a.activity}]` : a.state.toUpperCase()
    msgs.push(`${a.name}: ${label}`)
  })
  tasksStore.tasks
    .filter(t => !['done', 'failed', 'cancelled'].includes(t.state))
    .slice(0, 4)
    .forEach(t => msgs.push(`TASK: ${t.name || t.id}`))
  if (msgs.length === 0) msgs.push('AWAITING WORKERS', 'SYSTEMS NOMINAL', 'BOILER PRESSURE: 87 PSI')
  return msgs
})

function truncate(str?: string, len = 28) {
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
  background: linear-gradient(180deg, #0d0600 0%, #120900 50%, #1a0e00 100%);
  position: relative;
  overflow: hidden;
  font-family: var(--font-pixel);
}

/* Floor grid — warm brass tint, softer */
.floor-grid {
  position: absolute;
  inset: 0;
  background-image:
    linear-gradient(rgba(232, 170, 0, 0.04) 1px, transparent 1px),
    linear-gradient(90deg, rgba(232, 170, 0, 0.04) 1px, transparent 1px);
  background-size: 48px 48px;
  pointer-events: none;
}

/* Sparkles */
.sparkle-field {
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 1;
}
.sparkle {
  position: absolute;
  animation: sparkleFade 3s infinite ease-in-out;
}
.sparkle:nth-child(4n+1) { color: var(--color-gold); }
.sparkle:nth-child(4n+2) { color: var(--color-teal); }
.sparkle:nth-child(4n+3) { color: var(--color-amber); }
.sparkle:nth-child(4n)   { color: var(--color-cream); }

@keyframes sparkleFade {
  0%, 100% { opacity: 0;    transform: scale(0.3) rotate(0deg); }
  40%, 60% { opacity: 0.75; transform: scale(1.05) rotate(180deg); }
}

/* Ceiling pipe — single horizontal run */
.ceiling-pipe {
  position: absolute;
  top: 18px;
  left: 0;
  right: 0;
  height: 10px;
  background: linear-gradient(180deg, #6e3500 0%, #b34a00 35%, #d76420 55%, #7a3c00 100%);
  border-top: 1px solid #5a2a00;
  border-bottom: 1px solid #5a2a00;
  box-shadow: 0 0 6px rgba(204, 85, 0, 0.4);
  z-index: 2;
}

.pipe-joint {
  position: absolute;
  top: 14px;
  width: 18px;
  height: 18px;
  background: radial-gradient(circle, var(--color-brass-light) 20%, var(--color-brass) 60%, var(--color-brass-dark) 100%);
  border: 2px solid var(--color-brass-dark);
  border-radius: 50%;
  box-shadow: 0 0 5px rgba(232, 170, 0, 0.5);
  z-index: 3;
}
.j1 { left: 22%; }
.j2 { left: 64%; }

/* Two soft steam vents above the pipe */
.steam-vent {
  position: absolute;
  width: 12px;
  z-index: 2;
}
.vent1 { top: 30px; left: calc(22% + 5px); }
.vent2 { top: 30px; left: calc(64% + 5px); }

.steam-particle {
  position: absolute;
  width: 8px;
  height: 8px;
  background: radial-gradient(circle, rgba(255, 232, 176, 0.7) 0%, transparent 70%);
  border-radius: 50%;
  animation: steamRise 2.4s var(--delay, 0s) infinite ease-out;
}

@keyframes steamRise {
  0%   { transform: translateY(0) scale(0.4); opacity: 0.75; }
  60%  { transform: translateY(-22px) scale(1.4) translateX(4px); opacity: 0.4; }
  100% { transform: translateY(-44px) scale(1.9) translateX(-4px); opacity: 0; }
}

/* Two distant ambient gears (chat side) */
.ambient-gear {
  position: absolute;
  user-select: none;
  pointer-events: none;
  line-height: 1;
}
.ambient-gear.g1 {
  right: 26px;
  top: 56px;
  font-size: 48px;
  color: var(--color-gold);
  text-shadow: 0 0 10px var(--color-gold), 0 0 18px rgba(255,214,68,0.35);
  animation: gearSpin 9s linear infinite;
}
.ambient-gear.g2 {
  right: 70px;
  top: 90px;
  font-size: 30px;
  color: var(--color-teal);
  text-shadow: 0 0 8px var(--color-teal), 0 0 16px rgba(0,187,170,0.3);
  animation: gearSpin 5s linear infinite reverse;
}

@keyframes gearSpin {
  from { transform: rotate(0deg); }
  to   { transform: rotate(360deg); }
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
    var(--color-amber));
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

/* ── Tasks table ─────────────────────────────────────────── */
.task-table {
  position: absolute;
  display: flex;
  flex-direction: column;
  gap: 8px;
  z-index: 3;
}

.task-row {
  position: relative;
  background:
    linear-gradient(180deg, rgba(36, 18, 4, 0.55) 0%, rgba(20, 10, 2, 0.85) 100%);
  border: 2px solid var(--color-brass-dark);
  border-radius: 3px;
  box-shadow:
    0 0 12px rgba(232, 170, 0, 0.08),
    inset 0 0 14px rgba(255, 119, 0, 0.04);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  transition: border-color 0.4s, box-shadow 0.4s;
}

.task-row.occupied {
  border-color: var(--color-brass);
  box-shadow:
    0 0 14px rgba(232, 170, 0, 0.18),
    inset 0 0 14px rgba(255, 119, 0, 0.08);
}

/* State accents on the row */
.task-row.state-working   { border-color: var(--color-green); box-shadow: 0 0 14px rgba(136,221,34,0.22), inset 0 0 12px rgba(136,221,34,0.05); }
.task-row.state-planning  { border-color: var(--color-sky);   box-shadow: 0 0 14px rgba(68,170,238,0.22), inset 0 0 12px rgba(68,170,238,0.05); }
.task-row.state-followup  { border-color: var(--color-orange);box-shadow: 0 0 14px rgba(255,119,0,0.25),  inset 0 0 12px rgba(255,119,0,0.06); }
.task-row.state-awaiting-review { border-color: var(--color-amber); box-shadow: 0 0 14px rgba(255,204,0,0.25), inset 0 0 12px rgba(255,204,0,0.06); }

.row-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px 10px;
  background: linear-gradient(90deg, rgba(232,170,0,0.10) 0%, rgba(232,170,0,0.02) 60%, transparent 100%);
  border-bottom: 1px solid rgba(232, 170, 0, 0.2);
  font-size: 7px;
  letter-spacing: 1.5px;
  flex-shrink: 0;
  height: 18px;
}

.row-num {
  font-family: var(--font-pixel);
  color: var(--color-brass-dark);
  background: rgba(0, 0, 0, 0.5);
  padding: 1px 4px;
  border: 1px solid var(--color-brass-dark);
  border-radius: 1px;
}

.row-title {
  flex: 1;
  color: var(--color-cream);
  text-shadow: 0 0 5px rgba(255,232,176,0.4);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.task-row:not(.occupied) .row-title {
  color: var(--color-text-dim);
  font-style: italic;
  text-shadow: none;
}

.row-state {
  padding: 1px 6px;
  border-radius: 2px;
  letter-spacing: 1px;
  text-transform: uppercase;
  font-size: 6px;
  background: rgba(0,0,0,0.45);
  border: 1px solid currentColor;
}

.state-working       { color: var(--color-green);  text-shadow: 0 0 4px var(--color-green); }
.state-pending       { color: var(--color-text-dim); }
.state-planning      { color: var(--color-sky);    text-shadow: 0 0 4px var(--color-sky); }
.state-awaiting-review { color: var(--color-amber); text-shadow: 0 0 4px var(--color-amber); }
.state-followup      { color: var(--color-orange); text-shadow: 0 0 4px var(--color-orange); }
.state-failed        { color: var(--color-red);    text-shadow: 0 0 4px var(--color-red); }
.state-done          { color: var(--color-teal);   text-shadow: 0 0 4px var(--color-teal); }

/* The bench area (where stations live and agents stand) */
.row-bench {
  position: relative;
  flex: 1;
  background:
    linear-gradient(180deg,
      rgba(90, 56, 24, 0.0) 0%,
      rgba(90, 56, 24, 0.0) 55%,
      rgba(90, 56, 24, 0.55) 56%,
      rgba(58, 32, 14, 0.85) 100%);
}

/* Brass rail along the front of the bench */
.bench-rail {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 14px;
  height: 4px;
  background: linear-gradient(180deg, var(--color-brass-light) 0%, var(--color-brass) 50%, var(--color-brass-dark) 100%);
  box-shadow: 0 0 6px rgba(232, 170, 0, 0.4);
}

.bench-bolts {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 4px;
  height: 8px;
  display: flex;
  justify-content: space-between;
  padding: 0 8px;
  pointer-events: none;
}
.bolt {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: radial-gradient(circle, var(--color-brass-light) 30%, var(--color-brass-dark) 100%);
  box-shadow: 0 0 3px rgba(232, 170, 0, 0.5);
}

/* Stations — pegged into the bench */
.bench-station {
  position: absolute;
  bottom: 16px;
  transform: translateX(-50%);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  opacity: 0.55;
  filter: saturate(0.7);
  transition: opacity 0.3s, filter 0.3s, transform 0.3s;
  pointer-events: none;
}
.bench-station.active {
  opacity: 1;
  filter: saturate(1.15) drop-shadow(0 0 6px rgba(255, 214, 68, 0.45));
  transform: translateX(-50%) translateY(-2px);
}

.station-icon {
  position: relative;
  width: 28px;
  height: 28px;
  display: flex;
  align-items: flex-end;
  justify-content: center;
}

.station-label {
  font-size: 4.5px;
  letter-spacing: 1.2px;
  color: var(--color-brass-dark);
  text-shadow: 0 0 3px rgba(232, 170, 0, 0.4);
  white-space: nowrap;
}
.bench-station.active .station-label {
  color: var(--color-cream);
  text-shadow: 0 0 4px rgba(255, 232, 176, 0.7);
}

/* ── Mini POI graphics ───────────────────────────────────── */

/* Archive — stacked colored books */
.station-archive .station-icon {
  gap: 1px;
  align-items: flex-end;
  justify-content: center;
}
.mini-book {
  display: inline-block;
  width: 4px;
  border-radius: 1px 1px 0 0;
  border: 1px solid rgba(0,0,0,0.4);
  margin: 0 1px;
}
.mini-book.b1 { height: 16px; background: var(--color-teal);   box-shadow: 0 0 3px var(--color-teal); }
.mini-book.b2 { height: 12px; background: var(--color-amber);  box-shadow: 0 0 3px var(--color-amber); }
.mini-book.b3 { height: 18px; background: var(--color-sky);    box-shadow: 0 0 3px var(--color-sky); }
.mini-book.b4 { height: 14px; background: var(--color-orange); box-shadow: 0 0 3px var(--color-orange); }

/* Scrying orb */
.mini-orb-glow {
  position: absolute;
  inset: 4px;
  border-radius: 50%;
  background: radial-gradient(circle, rgba(136,68,255,0.45) 0%, transparent 70%);
  animation: orbPulse 2.2s ease-in-out infinite;
}
.mini-orb {
  position: relative;
  font-size: 16px;
  line-height: 1;
  filter: drop-shadow(0 0 5px rgba(136,68,255,0.85));
  animation: orbFloat 3s ease-in-out infinite;
}
@keyframes orbPulse {
  0%, 100% { opacity: 0.4; transform: scale(0.85); }
  50%       { opacity: 0.85; transform: scale(1.1); }
}
@keyframes orbFloat {
  0%, 100% { transform: translateY(0px); }
  50%       { transform: translateY(-2px); }
}

/* Telegraph */
.mini-tg-body {
  position: relative;
  width: 24px;
  height: 18px;
  background: linear-gradient(180deg, #2a1200 0%, #180900 100%);
  border: 1px solid var(--color-copper);
  border-radius: 2px;
  display: flex;
  align-items: flex-end;
  justify-content: center;
  padding-bottom: 2px;
}
.mini-tg-key {
  width: 10px;
  height: 4px;
  background: var(--color-brass-light);
  border-radius: 2px;
  border: 1px solid var(--color-brass-dark);
  animation: keyTap 1.6s ease-in-out infinite;
}
@keyframes keyTap {
  0%, 85%, 100% { transform: translateY(0); }
  90%            { transform: translateY(2px); }
}
.mini-tg-spark {
  position: absolute;
  top: 2px;
  right: 4px;
  width: 3px;
  height: 3px;
  background: var(--color-sky);
  border-radius: 50%;
  animation: tgSpark 1.6s ease-out infinite;
}
@keyframes tgSpark {
  0%   { opacity: 0; transform: translate(0, 0) scale(1); }
  30%  { opacity: 1; transform: translate(2px, -3px) scale(1.4); }
  100% { opacity: 0; transform: translate(5px, -8px) scale(0.2); }
}

/* Think tank */
.mini-tank {
  position: relative;
  width: 22px;
  height: 22px;
  background: linear-gradient(180deg, rgba(68,153,255,0.18) 0%, rgba(0,0,0,0) 100%);
  border: 1px solid rgba(68,153,255,0.5);
  border-radius: 3px 3px 1px 1px;
  overflow: hidden;
  box-shadow: 0 0 6px rgba(68,153,255,0.3), inset 0 0 5px rgba(68,153,255,0.15);
}
.mini-bubble {
  position: absolute;
  bottom: 2px;
  left: 50%;
  width: 4px;
  height: 4px;
  background: radial-gradient(circle, rgba(68,153,255,0.8) 0%, transparent 70%);
  border-radius: 50%;
  animation: miniBubble 2.2s var(--delay, 0s) ease-out infinite;
}
@keyframes miniBubble {
  0%   { transform: translate(-50%, 0) scale(0.6); opacity: 0.85; }
  100% { transform: translate(-50%, -22px) scale(1.6); opacity: 0; }
}

/* Engine — spinning gear */
.mini-engine {
  font-size: 22px;
  line-height: 1;
  color: var(--color-gold);
  text-shadow: 0 0 6px var(--color-gold);
  animation: gearSpin 4s linear infinite;
}

/* Forge — flickering flame */
.mini-forge {
  font-size: 18px;
  line-height: 1;
  filter: drop-shadow(0 0 5px rgba(255,100,0,0.85));
  animation: forgeFlicker 0.4s infinite alternate;
}
@keyframes forgeFlicker {
  from { opacity: 0.85; transform: scale(0.95); }
  to   { opacity: 1;    transform: scale(1.05); }
}

/* ── Break room ─────────────────────────────────────────── */
.break-room {
  position: absolute;
  height: 86px;
  border-top: 1px dashed rgba(232, 170, 0, 0.25);
  border-bottom: 1px dashed rgba(232, 170, 0, 0.15);
  background:
    repeating-linear-gradient(
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

/* ── Floating agents ──────────────────────────────────────── */
.floating-agent {
  position: absolute;
  z-index: 6;
  transform: translate(-50%, -50%);
  transition: left var(--walk-dur, 1.5s) ease-in-out, top var(--walk-dur, 1.5s) ease-in-out;
  display: flex;
  flex-direction: column;
  align-items: center;
  pointer-events: none;
}

/* ── Ticker tape ──────────────────────────────────────────── */
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
  z-index: 8;
}

.ticker-content {
  display: flex;
  animation: tickerScroll 22s linear infinite;
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
