<template>
  <div class="factory-floor">
    <!-- Background grid -->
    <div class="floor-grid"></div>

    <!-- Floating sparkles -->
    <div class="sparkle-field">
      <div
        class="sparkle"
        v-for="n in 18"
        :key="`sp${n}`"
        :style="`left: ${(n * 73 + 11) % 93}%; top: ${(n * 59 + 17) % 80}%; font-size: ${8 + (n % 4) * 3}px; animation-delay: ${((n * 0.37) % 2.8).toFixed(1)}s; animation-duration: ${2 + (n % 3) * 0.7}s;`"
      >{{ ['✦','★','✧','⋆','✩','❋'][n % 6] }}</div>
    </div>

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
        <div class="belt-item" v-for="(item, n) in beltItems" :key="n" :style="`--offset: ${n * 60}px`">{{ item }}</div>
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
      <span class="factory-title">✨ PIONEER SQUARE WORKSHOP ✨</span>
      <span class="agent-count">Agents: {{ agents.length }}</span>
    </div>

    <!-- Ticker tape -->
    <div class="ticker-tape">
      <div class="ticker-content">
        <span v-for="(msg, i) in tickerMessages" :key="i" class="ticker-msg">
          ★ {{ msg }}
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

const beltItems = ['🍬', '⭐', '💎', '🍭', '🌟', '🔮']

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
  if (msgs.length === 0) msgs.push('AWAITING AGENTS ✨', 'SYSTEMS NOMINAL 💎', 'ALL SYSTEMS GO 🌟')
  return msgs
})
</script>

<style scoped>
.factory-floor {
  width: 100%;
  height: 100%;
  background: linear-gradient(180deg, #050415 0%, #0a0918 40%, #0f0d22 100%);
  position: relative;
  overflow: hidden;
  font-family: var(--font-pixel);
}

/* Floor grid */
.floor-grid {
  position: absolute;
  inset: 0;
  background-image:
    linear-gradient(rgba(192, 122, 255, 0.06) 1px, transparent 1px),
    linear-gradient(90deg, rgba(192, 122, 255, 0.06) 1px, transparent 1px);
  background-size: 40px 40px;
}

/* Sparkle field */
.sparkle-field {
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 1;
}

.sparkle {
  position: absolute;
  animation: sparkleFade 2s infinite ease-in-out;
}

.sparkle:nth-child(6n+1) { color: var(--color-pink); }
.sparkle:nth-child(6n+2) { color: var(--color-yellow); }
.sparkle:nth-child(6n+3) { color: var(--color-cyan); }
.sparkle:nth-child(6n+4) { color: var(--color-mint); }
.sparkle:nth-child(6n+5) { color: var(--color-purple); }
.sparkle:nth-child(6n)   { color: var(--color-lavender); }

@keyframes sparkleFade {
  0%, 100% { opacity: 0; transform: scale(0.3) rotate(0deg); }
  40%, 60% { opacity: 0.9; transform: scale(1.1) rotate(180deg); }
}

/* Pipes */
.ceiling-pipe {
  position: absolute;
  background: linear-gradient(180deg, #7733bb 0%, #c07aff 40%, #5511aa 100%);
  border: 1px solid #7733bb;
  box-shadow: 0 0 6px rgba(192, 122, 255, 0.3);
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
  background: radial-gradient(circle, rgba(217, 166, 255, 0.8) 0%, transparent 70%);
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
  user-select: none;
  line-height: 1;
}

.gear-large { font-size: 56px; animation: gearSpin 8s linear infinite; }
.gear-medium { font-size: 36px; animation: gearSpin 5s linear infinite reverse; }
.gear-small { font-size: 22px; animation: gearSpin 3s linear infinite; }

.g1 { right: 30px; top: 60px; color: var(--color-pink); text-shadow: 0 0 10px var(--color-pink), 0 0 20px rgba(255, 107, 179, 0.4); }
.g2 { right: 75px; top: 80px; color: var(--color-cyan); text-shadow: 0 0 10px var(--color-cyan), 0 0 20px rgba(102, 204, 255, 0.4); }
.g3 { right: 55px; top: 100px; color: var(--color-yellow); text-shadow: 0 0 10px var(--color-yellow), 0 0 20px rgba(255, 229, 102, 0.4); }
.g4 { left: 620px; top: 40px; color: var(--color-mint); text-shadow: 0 0 10px var(--color-mint), 0 0 20px rgba(0, 255, 179, 0.4); }
.g5 { left: 650px; top: 65px; color: var(--color-purple); text-shadow: 0 0 10px var(--color-purple), 0 0 20px rgba(192, 122, 255, 0.4); }

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
  background: linear-gradient(180deg, #1a0a2e 0%, #0f0820 100%);
  border: 3px solid var(--color-copper);
  border-radius: 4px 4px 0 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: space-around;
  padding: 6px;
  box-shadow: 0 0 12px rgba(255, 107, 179, 0.3);
}

.furnace-door {
  width: 36px;
  height: 36px;
  background: #0f0818;
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
  background: #0f0818;
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
  box-shadow: 0 0 4px var(--color-red);
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
  background: linear-gradient(180deg, #1a0a2e 0%, #0f0820 100%);
  border: 2px solid var(--color-copper);
  border-bottom: none;
}

.smoke-particle {
  position: absolute;
  top: 0;
  left: 50%;
  width: 10px;
  height: 10px;
  background: radial-gradient(circle, rgba(180, 80, 220, 0.5) 0%, transparent 70%);
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
    #1a0f2e 0px,
    #1a0f2e 18px,
    #22143a 18px,
    #22143a 36px
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

.belt-item:nth-child(6n+1) { filter: drop-shadow(0 0 3px var(--color-pink)); }
.belt-item:nth-child(6n+2) { filter: drop-shadow(0 0 3px var(--color-yellow)); }
.belt-item:nth-child(6n+3) { filter: drop-shadow(0 0 3px var(--color-cyan)); }
.belt-item:nth-child(6n+4) { filter: drop-shadow(0 0 3px var(--color-mint)); }
.belt-item:nth-child(6n+5) { filter: drop-shadow(0 0 3px var(--color-purple)); }
.belt-item:nth-child(6n)   { filter: drop-shadow(0 0 3px var(--color-lavender)); }

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
  box-shadow: 0 0 6px rgba(255, 107, 179, 0.4);
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
  background: #0a0918;
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
  background: #050415;
  border: 2px solid #1a1530;
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
  opacity: 0.8;
  animation: screenScroll 2s infinite linear;
  border-radius: 1px;
}

.screen-line:nth-child(1) { background: var(--color-mint); }
.screen-line:nth-child(2) { background: var(--color-cyan); animation-delay: -0.7s; opacity: 0.5; }
.screen-line:nth-child(3) { background: var(--color-pink); animation-delay: -1.4s; opacity: 0.3; width: 60%; }

@keyframes screenScroll {
  0% { opacity: 0.8; }
  50% { opacity: 0.3; }
  100% { opacity: 0.8; }
}

.screen-idle {
  font-family: var(--font-pixel);
  font-size: 8px;
  color: #2a1a3a;
}

.station-table {
  width: 100%;
  height: 14px;
  background: linear-gradient(180deg, #2a1a4a 0%, #1a0f30 100%);
  border: 2px solid var(--color-copper-light);
  border-top: 3px solid var(--color-brass);
}

.station-agent, .station-empty {
  margin-top: 2px;
}

.empty-slot {
  width: 30px;
  height: 50px;
  background: rgba(192, 122, 255, 0.05);
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
  background: linear-gradient(180deg, #3a1a5a 0%, #22103e 100%);
  box-shadow: 0 0 6px rgba(192, 122, 255, 0.2);
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
  background: rgba(10, 9, 24, 0.88);
  border: 1px solid var(--color-brass);
  padding: 4px 14px;
  z-index: 10;
  box-shadow: 0 0 14px rgba(192, 122, 255, 0.35), inset 0 0 8px rgba(192, 122, 255, 0.08);
}

.factory-title {
  font-size: 7px;
  letter-spacing: 2px;
  background: linear-gradient(90deg, var(--color-pink), var(--color-yellow), var(--color-cyan), var(--color-mint), var(--color-purple));
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
  color: transparent;
  animation: rainbowShift 4s linear infinite;
  background-size: 200% auto;
}

@keyframes rainbowShift {
  from { background-position: 0% center; }
  to { background-position: 200% center; }
}

.agent-count {
  font-size: 7px;
  color: var(--color-mint);
  text-shadow: 0 0 6px var(--color-mint);
}

/* Ticker tape */
.ticker-tape {
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  height: 28px;
  background: linear-gradient(90deg, var(--color-bg-secondary), rgba(119, 51, 187, 0.3), var(--color-bg-secondary));
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
  color: var(--color-yellow);
  margin-right: 60px;
  letter-spacing: 1px;
  text-shadow: 0 0 4px var(--color-yellow);
}

.ticker-msg:nth-child(3n+1) { color: var(--color-pink); text-shadow: 0 0 4px var(--color-pink); }
.ticker-msg:nth-child(3n+2) { color: var(--color-cyan); text-shadow: 0 0 4px var(--color-cyan); }
.ticker-msg:nth-child(3n)   { color: var(--color-yellow); text-shadow: 0 0 4px var(--color-yellow); }

@keyframes tickerScroll {
  from { transform: translateX(100vw); }
  to { transform: translateX(-100%); }
}
</style>
