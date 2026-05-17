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
      <!-- Pixel-art per-state scenery (sits behind stations) -->
      <svg
        v-if="sceneState !== 'idle'"
        class="bench-scene"
        :class="`scene-${sceneState}`"
        viewBox="0 0 480 48"
        preserveAspectRatio="xMidYMax meet"
        shape-rendering="crispEdges"
        xmlns="http://www.w3.org/2000/svg"
        aria-hidden="true"
      >
        <!-- PLANNING — blueprint + pencil + ruler -->
        <g v-if="sceneState === 'planning'">
          <!-- blueprint sheet (integer coords, no rx) -->
          <rect x="170" y="6" width="84" height="38" class="bp-sheet" />
          <rect x="170" y="6" width="84" height="6" class="bp-band" />
          <!-- grid (integer ticks) -->
          <rect x="176" y="18" width="72" height="1" class="bp-grid" />
          <rect x="176" y="26" width="72" height="1" class="bp-grid" />
          <rect x="176" y="34" width="72" height="1" class="bp-grid" />
          <rect x="190" y="14" width="1" height="26" class="bp-grid" />
          <rect x="208" y="14" width="1" height="26" class="bp-grid" />
          <rect x="226" y="14" width="1" height="26" class="bp-grid" />
          <rect x="244" y="14" width="1" height="26" class="bp-grid" />
          <!-- pencil (stepped diagonal: 1-px rectangles) -->
          <rect x="262" y="14" width="3" height="3" class="pencil-tip" />
          <rect x="265" y="17" width="2" height="2" class="pencil-tip" />
          <rect x="267" y="19" width="14" height="3" class="pencil-body" />
          <rect x="281" y="19" width="3" height="3" class="pencil-eraser" />
          <!-- ruler -->
          <rect x="290" y="30" width="48" height="6" class="ruler" />
          <rect x="296" y="30" width="1" height="2" class="ruler-tick" />
          <rect x="304" y="30" width="1" height="2" class="ruler-tick" />
          <rect x="312" y="30" width="1" height="2" class="ruler-tick" />
          <rect x="320" y="30" width="1" height="2" class="ruler-tick" />
          <rect x="328" y="30" width="1" height="2" class="ruler-tick" />
        </g>

        <!-- WORKING — laptop screen with code + coffee mug -->
        <g v-if="sceneState === 'working'">
          <!-- laptop chassis -->
          <rect x="174" y="4" width="100" height="34" class="laptop-shell" />
          <rect x="178" y="8" width="92" height="26" class="laptop-screen" />
          <!-- code lines (staggered widths, all on integer y) -->
          <rect x="182" y="12" width="42" height="2" class="code-ln c1" />
          <rect x="182" y="16" width="60" height="2" class="code-ln c2" />
          <rect x="186" y="20" width="30" height="2" class="code-ln c3" />
          <rect x="182" y="24" width="48" height="2" class="code-ln c4" />
          <rect x="186" y="28" width="40" height="2" class="code-ln c5" />
          <!-- blinking cursor (steps(2) toggles visibility) -->
          <rect x="228" y="28" width="3" height="2" class="code-cursor" />
          <!-- laptop base + keyboard -->
          <rect x="168" y="38" width="112" height="4" class="laptop-base" />
          <rect x="180" y="40" width="88" height="1" class="kbd-row" />
          <!-- coffee mug -->
          <rect x="296" y="20" width="20" height="18" class="mug-body" />
          <rect x="296" y="20" width="20" height="3" class="mug-rim" />
          <rect x="316" y="24" width="3" height="8" class="mug-handle" />
          <rect x="319" y="25" width="1" height="6" class="mug-handle" />
          <!-- steam (3 stepped puffs, animated on steps(3)) -->
          <g class="steam">
            <rect x="300" y="14" width="2" height="2" />
            <rect x="304" y="10" width="2" height="2" />
            <rect x="308" y="6" width="2" height="2" />
          </g>
        </g>

        <!-- AWAITING-REVIEW — paper stack + desk lamp w/ light cone -->
        <g v-if="sceneState === 'awaiting-review'">
          <!-- paper stack (3 staggered sheets) -->
          <rect x="166" y="32" width="100" height="6" class="paper-bot" />
          <rect x="164" y="26" width="102" height="6" class="paper-mid" />
          <rect x="162" y="8" width="104" height="20" class="paper-top" />
          <!-- doc lines on top sheet (1-px tall, integer y) -->
          <rect x="168" y="12" width="80" height="1" class="doc-line" />
          <rect x="168" y="16" width="72" height="1" class="doc-line" />
          <rect x="168" y="20" width="78" height="1" class="doc-line" />
          <rect x="168" y="24" width="58" height="1" class="doc-line" />
          <!-- lamp light cone (pure rects, no gradient) -->
          <polygon points="296,16 320,16 326,40 290,40" class="lamp-cone" />
          <!-- lamp pole + shade -->
          <rect x="300" y="10" width="16" height="6" class="lamp-shade" />
          <rect x="306" y="16" width="4" height="22" class="lamp-pole" />
          <rect x="298" y="38" width="20" height="4" class="lamp-base" />
        </g>

        <!-- FOLLOWUP — three sticky notes at slight angles (stair-stepped) -->
        <g v-if="sceneState === 'followup'">
          <!-- left sticky -->
          <rect x="172" y="8" width="40" height="34" class="sticky-a" />
          <rect x="178" y="14" width="28" height="1" class="sticky-ln-a" />
          <rect x="178" y="20" width="24" height="1" class="sticky-ln-a" />
          <rect x="178" y="26" width="26" height="1" class="sticky-ln-a" />
          <!-- center sticky (tilted via stair-step rects, no rotate transform) -->
          <rect x="222" y="10" width="40" height="32" class="sticky-b" />
          <rect x="223" y="9" width="40" height="1" class="sticky-b" />
          <rect x="224" y="8" width="40" height="1" class="sticky-b" />
          <rect x="228" y="16" width="28" height="1" class="sticky-ln-b" />
          <rect x="228" y="22" width="32" height="1" class="sticky-ln-b" />
          <rect x="228" y="28" width="26" height="1" class="sticky-ln-b" />
          <!-- right sticky -->
          <rect x="272" y="6" width="40" height="36" class="sticky-c" />
          <rect x="278" y="12" width="28" height="1" class="sticky-ln-c" />
          <rect x="278" y="18" width="30" height="1" class="sticky-ln-c" />
          <rect x="278" y="24" width="24" height="1" class="sticky-ln-c" />
          <rect x="278" y="30" width="28" height="1" class="sticky-ln-c" />
        </g>

        <!-- DONE — big stepped checkmark + sparkles -->
        <g v-if="sceneState === 'done'">
          <!-- check stroke drawn as a staircase of 4×4 blocks (clearly pixelated) -->
          <g class="check-blocks">
            <rect x="160" y="22" width="4" height="4" />
            <rect x="164" y="26" width="4" height="4" />
            <rect x="168" y="30" width="4" height="4" />
            <rect x="172" y="34" width="4" height="4" />
            <rect x="176" y="30" width="4" height="4" />
            <rect x="180" y="26" width="4" height="4" />
            <rect x="184" y="22" width="4" height="4" />
            <rect x="188" y="18" width="4" height="4" />
            <rect x="192" y="14" width="4" height="4" />
            <rect x="196" y="10" width="4" height="4" />
            <rect x="200" y="6" width="4" height="4" />
          </g>
          <!-- sparkles (plus-sign pixels) -->
          <g class="sparkle s1">
            <rect x="240" y="10" width="2" height="6" />
            <rect x="238" y="12" width="6" height="2" />
          </g>
          <g class="sparkle s2">
            <rect x="280" y="28" width="2" height="6" />
            <rect x="278" y="30" width="6" height="2" />
          </g>
          <g class="sparkle s3">
            <rect x="320" y="14" width="2" height="6" />
            <rect x="318" y="16" width="6" height="2" />
          </g>
        </g>

        <!-- FAILED — crumpled papers + stepped X -->
        <g v-if="sceneState === 'failed'">
          <rect x="168" y="22" width="48" height="18" class="crumple" />
          <rect x="170" y="20" width="44" height="2" class="crumple" />
          <rect x="290" y="14" width="44" height="22" class="crumple" />
          <rect x="292" y="12" width="40" height="2" class="crumple" />
          <!-- stepped X mark (two staircase diagonals of 4×4) -->
          <g class="x-mark">
            <rect x="228" y="6" width="4" height="4" />
            <rect x="232" y="10" width="4" height="4" />
            <rect x="236" y="14" width="4" height="4" />
            <rect x="240" y="18" width="4" height="4" />
            <rect x="244" y="22" width="4" height="4" />
            <rect x="248" y="26" width="4" height="4" />
            <rect x="252" y="30" width="4" height="4" />
            <rect x="256" y="34" width="4" height="4" />
            <rect x="228" y="34" width="4" height="4" />
            <rect x="232" y="30" width="4" height="4" />
            <rect x="236" y="26" width="4" height="4" />
            <rect x="240" y="22" width="4" height="4" />
            <rect x="248" y="14" width="4" height="4" />
            <rect x="252" y="10" width="4" height="4" />
            <rect x="256" y="6" width="4" height="4" />
          </g>
        </g>

        <!-- PENDING — a single hourglass icon (stepped) -->
        <g v-if="sceneState === 'pending'">
          <rect x="226" y="8" width="28" height="2" class="hg-frame" />
          <rect x="226" y="36" width="28" height="2" class="hg-frame" />
          <rect x="228" y="10" width="24" height="2" class="hg-glass" />
          <rect x="230" y="12" width="20" height="2" class="hg-glass" />
          <rect x="232" y="14" width="16" height="2" class="hg-glass" />
          <rect x="234" y="16" width="12" height="2" class="hg-glass" />
          <rect x="236" y="18" width="8" height="2" class="hg-glass" />
          <rect x="238" y="20" width="4" height="2" class="hg-glass" />
          <rect x="238" y="22" width="4" height="2" class="hg-glass" />
          <rect x="236" y="24" width="8" height="2" class="hg-glass" />
          <rect x="234" y="26" width="12" height="2" class="hg-glass" />
          <rect x="232" y="28" width="16" height="2" class="hg-glass" />
          <rect x="230" y="30" width="20" height="2" class="hg-glass" />
          <rect x="228" y="32" width="24" height="2" class="hg-glass" />
          <!-- falling sand pixel -->
          <rect class="hg-sand" x="239" y="20" width="2" height="2" />
        </g>
      </svg>

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
import { computed } from 'vue'
import type { Task, TaskState } from '../../types'

const props = defineProps<{
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

type SceneState =
  | 'idle'
  | 'pending'
  | 'planning'
  | 'working'
  | 'awaiting-review'
  | 'followup'
  | 'done'
  | 'failed'

const STATE_TO_SCENE: Record<TaskState, SceneState> = {
  pending: 'pending',
  planning: 'planning',
  working: 'working',
  'awaiting-review': 'awaiting-review',
  followup: 'followup',
  done: 'done',
  failed: 'failed',
  cancelled: 'failed',
}

const sceneState = computed<SceneState>(() =>
  props.task ? STATE_TO_SCENE[props.task.state] : 'idle',
)

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

/* ── Per-state bench scenery (pixel-art SVG) ───────────────── */
.bench-scene {
  position: absolute;
  left: 0;
  right: 0;
  top: 0;
  bottom: 16px;
  width: 100%;
  height: calc(100% - 16px);
  pointer-events: none;
  opacity: 0.6;
  z-index: 0;
  image-rendering: pixelated;
}
.task-row.occupied .bench-scene {
  opacity: 0.78;
}
.bench-station,
.bench-rail,
.bench-bolts {
  z-index: 1;
}

/* PLANNING — blueprint scene */
.bp-sheet {
  fill: #0e2452;
}
.bp-band {
  fill: #1a3a72;
}
.bp-grid {
  fill: #4488bb;
}
.pencil-body {
  fill: #ffd644;
}
.pencil-tip {
  fill: #dd9944;
}
.pencil-eraser {
  fill: #ee5544;
}
.ruler {
  fill: #c8b88a;
}
.ruler-tick {
  fill: #5a3a18;
}

/* WORKING — laptop scene */
.laptop-shell {
  fill: #1a1a1a;
}
.laptop-screen {
  fill: #0a1a0a;
}
.laptop-base {
  fill: #222;
}
.kbd-row {
  fill: #444;
}
.code-ln {
  fill: #33aa44;
}
.code-ln.c2 {
  fill: #88dd22;
}
.code-ln.c3 {
  fill: #44aadd;
}
.code-ln.c5 {
  fill: #ffaa22;
}
.code-cursor {
  fill: #88dd22;
  animation: pxCursorBlink 0.9s steps(2, end) infinite;
}
@keyframes pxCursorBlink {
  0%,
  49% {
    opacity: 1;
  }
  50%,
  100% {
    opacity: 0;
  }
}
.mug-body {
  fill: #8b4513;
}
.mug-rim {
  fill: #5c2b08;
}
.mug-handle {
  fill: #6b3010;
}
.steam rect {
  fill: #c8b89a;
}
.scene-working .steam {
  animation: pxSteam 1.5s steps(3, end) infinite;
}
@keyframes pxSteam {
  0%,
  33% {
    opacity: 0.9;
    transform: translateY(0);
  }
  34%,
  66% {
    opacity: 0.6;
    transform: translateY(-2px);
  }
  67%,
  100% {
    opacity: 0.25;
    transform: translateY(-4px);
  }
}

/* AWAITING-REVIEW — paper + lamp */
.paper-top {
  fill: #e4d8bc;
}
.paper-mid {
  fill: #d4c4a4;
}
.paper-bot {
  fill: #c8b89a;
}
.doc-line {
  fill: #7a6a52;
}
.lamp-shade {
  fill: #cc9922;
}
.lamp-pole {
  fill: #aaa;
}
.lamp-base {
  fill: #888;
}
.lamp-cone {
  fill: #ffd644;
  opacity: 0.18;
  animation: pxLampFlicker 1.6s steps(2, end) infinite;
}
@keyframes pxLampFlicker {
  0%,
  49% {
    opacity: 0.18;
  }
  50%,
  100% {
    opacity: 0.12;
  }
}

/* FOLLOWUP — sticky notes */
.sticky-a {
  fill: #ffee55;
}
.sticky-b {
  fill: #ff9944;
}
.sticky-c {
  fill: #55ddbb;
}
.sticky-ln-a {
  fill: #bb9900;
}
.sticky-ln-b {
  fill: #aa5500;
}
.sticky-ln-c {
  fill: #228877;
}

/* DONE — stepped checkmark + sparkles */
.check-blocks rect {
  fill: #88dd22;
  opacity: 0;
  animation: pxCheckIn 0.8s steps(11, end) forwards;
}
.check-blocks rect:nth-child(1) {
  animation-delay: 0s;
}
.check-blocks rect:nth-child(2) {
  animation-delay: 0.06s;
}
.check-blocks rect:nth-child(3) {
  animation-delay: 0.12s;
}
.check-blocks rect:nth-child(4) {
  animation-delay: 0.18s;
}
.check-blocks rect:nth-child(5) {
  animation-delay: 0.24s;
}
.check-blocks rect:nth-child(6) {
  animation-delay: 0.3s;
}
.check-blocks rect:nth-child(7) {
  animation-delay: 0.36s;
}
.check-blocks rect:nth-child(8) {
  animation-delay: 0.42s;
}
.check-blocks rect:nth-child(9) {
  animation-delay: 0.48s;
}
.check-blocks rect:nth-child(10) {
  animation-delay: 0.54s;
}
.check-blocks rect:nth-child(11) {
  animation-delay: 0.6s;
}
@keyframes pxCheckIn {
  from {
    opacity: 0;
  }
  to {
    opacity: 1;
  }
}
.sparkle rect {
  fill: #ffd644;
}
.sparkle {
  animation: pxSparkle 1.4s steps(4, end) infinite;
}
.sparkle.s2 {
  animation-delay: 0.35s;
}
.sparkle.s3 {
  animation-delay: 0.7s;
}
@keyframes pxSparkle {
  0%,
  74% {
    opacity: 1;
  }
  75%,
  100% {
    opacity: 0;
  }
}

/* FAILED — crumpled paper + stepped X */
.crumple {
  fill: #b8a884;
}
.x-mark rect {
  fill: #ee3322;
}

/* PENDING — hourglass */
.hg-frame {
  fill: #c8b88a;
}
.hg-glass {
  fill: #2a1a08;
}
.hg-sand {
  fill: #ffd644;
  animation: pxHgSand 1.6s steps(8, end) infinite;
}
@keyframes pxHgSand {
  from {
    transform: translateY(0);
    opacity: 1;
  }
  to {
    transform: translateY(10px);
    opacity: 0;
  }
}
</style>
