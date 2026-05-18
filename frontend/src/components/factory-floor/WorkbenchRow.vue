<template>
  <div
    class="task-row"
    :class="{ occupied: !!task, [`state-${task?.state || 'empty'}`]: true }"
    :style="`height: ${rowHeight}px;`"
  >
    <div class="row-header">
      <div class="row-num">{{ String(index + 1).padStart(2, '0') }}</div>
      <div class="row-title">
        {{ task ? truncate(task.name || task.id, 28) : '— vacant bench —' }}
      </div>
      <div v-if="task" class="row-state" :class="`state-${task.state}`">
        {{ stateLabel(task.state) }}
      </div>
    </div>

    <div class="row-bench">
      <div class="bench-rail"></div>
      <div class="bench-bolts">
        <span class="bolt" v-for="n in 6" :key="n"></span>
      </div>

      <div
        v-for="st in stations"
        :key="st.key"
        class="bench-station"
        :class="[`station-${st.key}`, { active: activityKey === st.key }]"
        :style="`left: ${st.frac * 100}%`"
      >
        <div class="station-icon">
          <template v-if="st.component === 'archive'">
            <span class="mini-book b1"></span>
            <span class="mini-book b2"></span>
            <span class="mini-book b3"></span>
            <span class="mini-book b4"></span>
          </template>
          <template v-else-if="st.component === 'orb'">
            <span class="mini-orb-glow"></span>
            <span class="mini-orb">🔮</span>
          </template>
          <template v-else-if="st.component === 'telegraph'">
            <span class="mini-tg-body">
              <span class="mini-tg-key"></span>
              <span class="mini-tg-spark"></span>
            </span>
          </template>
          <template v-else-if="st.component === 'tank'">
            <span class="mini-tank">
              <span
                class="mini-bubble"
                v-for="n in 2"
                :key="n"
                :style="`--delay: ${n * 0.4}s`"
              ></span>
            </span>
          </template>
          <template v-else-if="st.component === 'engine'">
            <span class="mini-engine">⚙</span>
          </template>
          <template v-else-if="st.component === 'forge'">
            <span class="mini-forge">🔥</span>
          </template>
        </div>
        <div class="station-label">{{ st.label }}</div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { Task } from '../../types'

defineProps<{
  task: Task | null
  index: number
  rowHeight: number
  activityKey: string | null
  stations: ReadonlyArray<{
    key: string
    label: string
    frac: number
    component: string
  }>
  stateLabel: (state: string) => string
}>()

function truncate(str?: string, len = 28) {
  if (!str) return ''
  return str.length > len ? str.slice(0, len) + '…' : str
}
</script>

<style scoped>
.task-row {
  position: relative;
  background: linear-gradient(180deg, rgba(36, 18, 4, 0.55) 0%, rgba(20, 10, 2, 0.85) 100%);
  border: 2px solid var(--color-brass-dark);
  border-radius: 3px;
  box-shadow:
    0 0 12px rgba(232, 170, 0, 0.08),
    inset 0 0 14px rgba(255, 119, 0, 0.04);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  transition:
    border-color 0.4s,
    box-shadow 0.4s;
}

.task-row.occupied {
  border-color: var(--color-brass);
  box-shadow:
    0 0 14px rgba(232, 170, 0, 0.18),
    inset 0 0 14px rgba(255, 119, 0, 0.08);
}

.task-row.state-working {
  border-color: var(--color-green);
  box-shadow:
    0 0 14px rgba(136, 221, 34, 0.22),
    inset 0 0 12px rgba(136, 221, 34, 0.05);
}
.task-row.state-planning {
  border-color: var(--color-sky);
  box-shadow:
    0 0 14px rgba(68, 170, 238, 0.22),
    inset 0 0 12px rgba(68, 170, 238, 0.05);
}
.task-row.state-followup {
  border-color: var(--color-orange);
  box-shadow:
    0 0 14px rgba(255, 119, 0, 0.25),
    inset 0 0 12px rgba(255, 119, 0, 0.06);
}
.task-row.state-awaiting-review {
  border-color: var(--color-amber);
  box-shadow:
    0 0 14px rgba(255, 204, 0, 0.25),
    inset 0 0 12px rgba(255, 204, 0, 0.06);
}

.row-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px 10px;
  background: linear-gradient(
    90deg,
    rgba(232, 170, 0, 0.1) 0%,
    rgba(232, 170, 0, 0.02) 60%,
    transparent 100%
  );
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
  text-shadow: 0 0 5px rgba(255, 232, 176, 0.4);
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
  background: rgba(0, 0, 0, 0.45);
  border: 1px solid currentColor;
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

.row-bench {
  position: relative;
  flex: 1;
  background: linear-gradient(
    180deg,
    rgba(90, 56, 24, 0) 0%,
    rgba(90, 56, 24, 0) 55%,
    rgba(90, 56, 24, 0.55) 56%,
    rgba(58, 32, 14, 0.85) 100%
  );
}

.bench-rail {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 14px;
  height: 4px;
  background: linear-gradient(
    180deg,
    var(--color-brass-light) 0%,
    var(--color-brass) 50%,
    var(--color-brass-dark) 100%
  );
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
  transition:
    opacity 0.3s,
    filter 0.3s,
    transform 0.3s;
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
.station-archive .station-icon {
  gap: 1px;
  align-items: flex-end;
  justify-content: center;
}
.mini-book {
  display: inline-block;
  width: 4px;
  border-radius: 1px 1px 0 0;
  border: 1px solid rgba(0, 0, 0, 0.4);
  margin: 0 1px;
}
.mini-book.b1 {
  height: 16px;
  background: var(--color-teal);
  box-shadow: 0 0 3px var(--color-teal);
}
.mini-book.b2 {
  height: 12px;
  background: var(--color-amber);
  box-shadow: 0 0 3px var(--color-amber);
}
.mini-book.b3 {
  height: 18px;
  background: var(--color-sky);
  box-shadow: 0 0 3px var(--color-sky);
}
.mini-book.b4 {
  height: 14px;
  background: var(--color-orange);
  box-shadow: 0 0 3px var(--color-orange);
}

.mini-orb-glow {
  position: absolute;
  inset: 4px;
  border-radius: 50%;
  background: radial-gradient(circle, rgba(136, 68, 255, 0.45) 0%, transparent 70%);
  animation: orbPulse 2.2s ease-in-out infinite;
}
.mini-orb {
  position: relative;
  font-size: 16px;
  line-height: 1;
  filter: drop-shadow(0 0 5px rgba(136, 68, 255, 0.85));
  animation: orbFloat 3s ease-in-out infinite;
}
@keyframes orbPulse {
  0%,
  100% {
    opacity: 0.4;
    transform: scale(0.85);
  }
  50% {
    opacity: 0.85;
    transform: scale(1.1);
  }
}
@keyframes orbFloat {
  0%,
  100% {
    transform: translateY(0px);
  }
  50% {
    transform: translateY(-2px);
  }
}

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
  0%,
  85%,
  100% {
    transform: translateY(0);
  }
  90% {
    transform: translateY(2px);
  }
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
  0% {
    opacity: 0;
    transform: translate(0, 0) scale(1);
  }
  30% {
    opacity: 1;
    transform: translate(2px, -3px) scale(1.4);
  }
  100% {
    opacity: 0;
    transform: translate(5px, -8px) scale(0.2);
  }
}

.mini-tank {
  position: relative;
  width: 22px;
  height: 22px;
  background: linear-gradient(180deg, rgba(68, 153, 255, 0.18) 0%, rgba(0, 0, 0, 0) 100%);
  border: 1px solid rgba(68, 153, 255, 0.5);
  border-radius: 3px 3px 1px 1px;
  overflow: hidden;
  box-shadow:
    0 0 6px rgba(68, 153, 255, 0.3),
    inset 0 0 5px rgba(68, 153, 255, 0.15);
}
.mini-bubble {
  position: absolute;
  bottom: 2px;
  left: 50%;
  width: 4px;
  height: 4px;
  background: radial-gradient(circle, rgba(68, 153, 255, 0.8) 0%, transparent 70%);
  border-radius: 50%;
  animation: miniBubble 2.2s var(--delay, 0s) ease-out infinite;
}
@keyframes miniBubble {
  0% {
    transform: translate(-50%, 0) scale(0.6);
    opacity: 0.85;
  }
  100% {
    transform: translate(-50%, -22px) scale(1.6);
    opacity: 0;
  }
}

.mini-engine {
  font-size: 22px;
  line-height: 1;
  color: var(--color-gold);
  text-shadow: 0 0 6px var(--color-gold);
  animation: gearSpin 4s linear infinite;
}

.mini-forge {
  font-size: 18px;
  line-height: 1;
  filter: drop-shadow(0 0 5px rgba(255, 100, 0, 0.85));
  animation: forgeFlicker 0.4s infinite alternate;
}
@keyframes forgeFlicker {
  from {
    opacity: 0.85;
    transform: scale(0.95);
  }
  to {
    opacity: 1;
    transform: scale(1.05);
  }
}

@keyframes gearSpin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}
</style>
