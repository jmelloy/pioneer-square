<template>
  <div class="factory-floor">
    <!-- Background grid -->
    <div class="floor-grid"></div>

    <!-- Ceiling pipes -->
    <div class="ceiling-pipe pipe-h pipe1"></div>
    <div class="ceiling-pipe pipe-h pipe2"></div>
    <div class="ceiling-pipe pipe-v pipe3"></div>
    <div class="ceiling-pipe pipe-v pipe4"></div>

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
        <div class="belt-item" v-for="n in 6" :key="n" :style="`--offset: ${n * 60}px`">⬡</div>
      </div>
      <div class="belt-roller left"></div>
      <div class="belt-roller right"></div>
    </div>

    <!-- Work stations -->
    <div
      v-for="(station, i) in stations"
      :key="i"
      class="work-station"
      :style="`left: ${station.x}px; top: ${station.y}px`"
      :class="{ occupied: station.agent }"
    >
      <div class="station-desk">
        <div class="station-monitor">
          <div class="monitor-screen">
            <div v-if="station.agent" class="screen-active">
              <div class="screen-line" v-for="l in 3" :key="l"></div>
            </div>
            <div v-else class="screen-idle">--</div>
          </div>
        </div>
        <div class="station-table"></div>
      </div>
      <div v-if="station.agent" class="station-agent">
        <AgentAvatar :agent="station.agent" />
      </div>
      <div v-else class="station-empty">
        <div class="empty-slot">?</div>
      </div>
      <div class="station-label">WS-{{ i + 1 }}</div>
    </div>

    <!-- Info overlay -->
    <div class="factory-info">
      <span class="factory-title">PIONEER SQUARE WORKSHOP</span>
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

<script setup>
import { computed, ref } from 'vue'
import { useAgentsStore } from '../stores/agents.js'
import AgentAvatar from './AgentAvatar.vue'

const agentsStore = useAgentsStore()
const agents = computed(() => agentsStore.agents)

const stationPositions = [
  { x: 60, y: 120 },
  { x: 200, y: 120 },
  { x: 340, y: 120 },
  { x: 480, y: 120 },
  { x: 60, y: 280 },
  { x: 200, y: 280 },
  { x: 340, y: 280 },
  { x: 480, y: 280 },
]

const stations = computed(() => {
  return stationPositions.map((pos, i) => ({
    ...pos,
    agent: agents.value[i] || null
  }))
})

const tickerMessages = computed(() => {
  const msgs = []
  agents.value.forEach(a => {
    msgs.push(`${a.name}: ${a.state.toUpperCase()}`)
  })
  if (msgs.length === 0) msgs.push('AWAITING AGENTS', 'SYSTEMS NOMINAL', 'BOILER PRESSURE: 87 PSI')
  return msgs
})
</script>

<style scoped>
.factory-floor {
  width: 100%;
  height: 100%;
  background: linear-gradient(180deg, #0d0800 0%, #1a0e00 40%, #2a1800 100%);
  position: relative;
  overflow: hidden;
  font-family: var(--font-pixel);
}

/* Floor grid */
.floor-grid {
  position: absolute;
  inset: 0;
  background-image:
    linear-gradient(rgba(181,134,13,0.06) 1px, transparent 1px),
    linear-gradient(90deg, rgba(181,134,13,0.06) 1px, transparent 1px);
  background-size: 40px 40px;
}

/* Pipes */
.ceiling-pipe {
  position: absolute;
  background: linear-gradient(180deg, #8a6300 0%, #b5860d 40%, #6a4800 100%);
  border: 1px solid var(--color-brass-dark);
}

.pipe-h { height: 12px; }
.pipe-v { width: 12px; }

.pipe1 { top: 20px; left: 0; right: 0; }
.pipe2 { top: 50px; left: 100px; width: 200px; }
.pipe3 { top: 0; left: 150px; height: 80px; }
.pipe4 { top: 0; left: 400px; height: 60px; }

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
  background: radial-gradient(circle, rgba(200,191,176,0.8) 0%, transparent 70%);
  border-radius: 50%;
  animation: steamRise 2s var(--delay, 0s) infinite ease-out;
}

@keyframes steamRise {
  0% { transform: translateY(0) scale(0.5); opacity: 0.8; }
  50% { transform: translateY(-30px) scale(1.5) translateX(5px); opacity: 0.4; }
  100% { transform: translateY(-60px) scale(2) translateX(-5px); opacity: 0; }
}

/* Gears */
.gear {
  position: absolute;
  color: var(--color-brass);
  text-shadow: 0 0 4px var(--color-brass-dark);
  user-select: none;
  line-height: 1;
}

.gear-large { font-size: 56px; animation: gearSpin 8s linear infinite; }
.gear-medium { font-size: 36px; animation: gearSpin 5s linear infinite reverse; }
.gear-small { font-size: 22px; animation: gearSpin 3s linear infinite; }

.g1 { right: 30px; top: 60px; }
.g2 { right: 75px; top: 80px; }
.g3 { right: 55px; top: 100px; }
.g4 { left: 620px; top: 40px; }
.g5 { left: 650px; top: 65px; }

@keyframes gearSpin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
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
  background: linear-gradient(180deg, #3a2010 0%, #2a1505 100%);
  border: 3px solid var(--color-copper);
  border-radius: 4px 4px 0 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: space-around;
  padding: 6px;
}

.furnace-door {
  width: 36px;
  height: 36px;
  background: #1a0800;
  border: 2px solid var(--color-copper-light);
  border-radius: 2px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 22px;
  animation: flicker 0.3s infinite alternate;
}

@keyframes flicker {
  from { opacity: 0.9; }
  to { opacity: 1; }
}

.furnace-gauge {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: #1a0800;
  border: 2px solid var(--color-brass);
  position: relative;
  overflow: hidden;
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
}

@keyframes needleSpin {
  from { transform: rotate(-60deg); }
  to { transform: rotate(60deg); }
}

.furnace-chimney {
  position: absolute;
  top: -30px;
  left: 50%;
  transform: translateX(-50%);
  width: 16px;
  height: 30px;
  background: linear-gradient(180deg, #3a2010 0%, #2a1505 100%);
  border: 2px solid var(--color-copper);
  border-bottom: none;
}

.smoke-particle {
  position: absolute;
  top: 0;
  left: 50%;
  width: 10px;
  height: 10px;
  background: radial-gradient(circle, rgba(100,80,60,0.8) 0%, transparent 70%);
  border-radius: 50%;
  animation: smokeRise 3s var(--delay, 0s) infinite ease-out;
}

@keyframes smokeRise {
  0% { transform: translate(-50%, 0) scale(0.5); opacity: 0.8; }
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
    #3a2510 0px,
    #3a2510 18px,
    #4a3520 18px,
    #4a3520 36px
  );
  border: 2px solid var(--color-copper-light);
  position: relative;
  overflow: hidden;
}

.belt-item {
  position: absolute;
  color: var(--color-brass);
  font-size: 12px;
  top: 2px;
  animation: beltMove 4s linear infinite;
  animation-delay: calc(var(--offset, 0) * -1ms / 10);
}

@keyframes beltMove {
  from { transform: translateX(120%); }
  to { transform: translateX(-100px); }
}

.belt-roller {
  position: absolute;
  bottom: 0;
  width: 28px;
  height: 28px;
  background: radial-gradient(circle, var(--color-copper) 30%, var(--color-copper-light) 60%, var(--color-copper) 100%);
  border-radius: 50%;
  border: 3px solid var(--color-brass-dark);
}

.belt-roller.left { left: -14px; }
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
  background: #1a0e00;
  border: 3px solid var(--color-brass-dark);
  border-radius: 2px;
  margin: 0 auto 0;
  display: flex;
  align-items: stretch;
  overflow: hidden;
  position: relative;
}

.monitor-screen {
  flex: 1;
  background: #050300;
  border: 2px solid #2a1a05;
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
  background: var(--color-green);
  opacity: 0.8;
  animation: screenScroll 2s infinite linear;
  border-radius: 1px;
}

.screen-line:nth-child(2) { animation-delay: -0.7s; opacity: 0.5; }
.screen-line:nth-child(3) { animation-delay: -1.4s; opacity: 0.3; width: 60%; }

@keyframes screenScroll {
  0% { opacity: 0.8; }
  50% { opacity: 0.3; }
  100% { opacity: 0.8; }
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
  background: rgba(181, 134, 13, 0.05);
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
}

.work-station.occupied .station-table {
  border-top-color: var(--color-brass-light);
  background: linear-gradient(180deg, #6a4820 0%, #4a2e12 100%);
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
  background: rgba(26, 14, 0, 0.8);
  border: 1px solid var(--color-brass-dark);
  padding: 4px 12px;
  z-index: 10;
}

.factory-title {
  font-size: 7px;
  color: var(--color-brass-light);
  letter-spacing: 2px;
}

.agent-count {
  font-size: 7px;
  color: var(--color-green);
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
}

@keyframes tickerScroll {
  from { transform: translateX(100vw); }
  to { transform: translateX(-100%); }
}
</style>
