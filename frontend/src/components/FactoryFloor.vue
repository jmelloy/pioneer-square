<template>
  <div class="factory-floor">
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
        <div class="worker-indicator" :class="station.agent.state">
          <div class="wi-screen">
            <div class="wi-line" v-for="l in 3" :key="l"></div>
          </div>
          <div class="wi-label">{{ station.agent.name.replace(/\/\d+$/, '') }}</div>
        </div>
      </div>
      <div v-else class="station-empty">
        <div class="empty-slot">?</div>
      </div>
      <div class="station-label">WS-{{ i + 1 }}</div>
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

<script setup>
import { computed } from 'vue'
import { useAgentsStore } from '../stores/agents.js'

const agentsStore = useAgentsStore()
const agents = computed(() => agentsStore.agents)

const beltItems = ['🔩', '⚙️', '🔧', '🪙', '⭐', '🔨']

const stationPositions = [
  { x: 60,  y: 120 },
  { x: 200, y: 120 },
  { x: 340, y: 120 },
  { x: 480, y: 120 },
  { x: 60,  y: 280 },
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
  if (msgs.length === 0) msgs.push('AWAITING WORKERS', 'SYSTEMS NOMINAL', 'BOILER PRESSURE: 87 PSI')
  return msgs
})
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

/* Teal/cyan screens — very Lucca's workshop */
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

.worker-indicator {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 3px;
}

.wi-screen {
  width: 36px;
  height: 28px;
  background: #080400;
  border: 2px solid var(--color-brass-dark);
  border-radius: 2px;
  padding: 4px 3px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.wi-line {
  height: 3px;
  border-radius: 1px;
  background: var(--color-text-dim);
  opacity: 0.4;
}

.worker-indicator.working .wi-line { background: var(--color-green); opacity: 0.85; animation: screenScroll 1.5s infinite linear; }
.worker-indicator.working .wi-line:nth-child(2) { animation-delay: -0.5s; opacity: 0.5; }
.worker-indicator.working .wi-line:nth-child(3) { animation-delay: -1s; opacity: 0.3; width: 60%; }
.worker-indicator.thinking .wi-line { background: var(--color-blue); opacity: 0.7; animation: screenScroll 2s infinite linear; }
.worker-indicator.busy .wi-line { background: var(--color-orange); opacity: 0.7; animation: screenScroll 1s infinite linear; }
.worker-indicator.error .wi-line { background: var(--color-red); opacity: 0.8; }

@keyframes screenScroll {
  0%, 100% { opacity: 0.85; }
  50%       { opacity: 0.2; }
}

.wi-label {
  font-family: var(--font-pixel);
  font-size: 5px;
  color: var(--color-brass-dark);
  text-align: center;
  max-width: 60px;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
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
  background: rgba(12, 7, 0, 0.88);
  border: 1px solid var(--color-brass);
  padding: 4px 14px;
  z-index: 10;
  box-shadow: 0 0 12px rgba(232, 170, 0, 0.3), inset 0 0 6px rgba(232, 170, 0, 0.06);
}

.factory-title {
  font-size: 7px;
  letter-spacing: 2px;
  /* Warm gold shimmer — very SNES RPG title feel */
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
