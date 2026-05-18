<template>
  <div class="station-screen" :class="[`state-${effectiveState}`]">
    <svg viewBox="0 0 60 48" preserveAspectRatio="xMidYMid meet" class="screen-svg">
      <!-- pending — hourglass -->
      <template v-if="effectiveState === 'pending'">
        <path
          d="M22 12 H38 L32 22 L38 32 H22 L28 22 Z"
          class="ico-stroke"
          fill="none"
        />
        <rect x="28" y="20" width="4" height="4" class="ico-accent" />
      </template>

      <!-- planning — blueprint sheet with grid + ruler -->
      <template v-else-if="effectiveState === 'planning'">
        <rect x="14" y="10" width="32" height="28" class="ico-bp-bg" />
        <line x1="14" y1="14" x2="46" y2="14" class="ico-stroke" />
        <line x1="20" y1="10" x2="20" y2="38" class="ico-grid" />
        <line x1="30" y1="10" x2="30" y2="38" class="ico-grid" />
        <line x1="40" y1="10" x2="40" y2="38" class="ico-grid" />
        <line x1="14" y1="22" x2="46" y2="22" class="ico-grid" />
        <line x1="14" y1="30" x2="46" y2="30" class="ico-grid" />
      </template>

      <!-- working — scrolling code lines (matches the old default) -->
      <template v-else-if="effectiveState === 'working'">
        <rect x="10" y="14" width="40" height="2" class="ico-line line1" />
        <rect x="10" y="20" width="28" height="2" class="ico-line line2" />
        <rect x="10" y="26" width="36" height="2" class="ico-line line3" />
        <rect x="10" y="32" width="22" height="2" class="ico-line line4" />
      </template>

      <!-- awaiting-review — clipboard with check rows -->
      <template v-else-if="effectiveState === 'awaiting-review'">
        <rect x="18" y="10" width="24" height="28" class="ico-paper" />
        <rect x="24" y="8" width="12" height="4" class="ico-clip" />
        <rect x="22" y="16" width="4" height="2" class="ico-accent" />
        <rect x="28" y="16" width="12" height="2" class="ico-stroke" />
        <rect x="22" y="22" width="4" height="2" class="ico-accent" />
        <rect x="28" y="22" width="12" height="2" class="ico-stroke" />
        <rect x="22" y="28" width="4" height="2" class="ico-accent" />
        <rect x="28" y="28" width="10" height="2" class="ico-stroke" />
      </template>

      <!-- followup — fanned sticky notes -->
      <template v-else-if="effectiveState === 'followup'">
        <rect x="12" y="14" width="16" height="16" class="ico-note note-a" />
        <rect x="22" y="12" width="16" height="16" class="ico-note note-b" />
        <rect x="32" y="16" width="16" height="16" class="ico-note note-c" />
      </template>

      <!-- done — big stepped checkmark -->
      <template v-else-if="effectiveState === 'done'">
        <path
          d="M14 24 L20 30 L28 22 L36 14 L46 16"
          class="ico-check"
          fill="none"
        />
      </template>

      <!-- failed — red X over a small rect (a crashed terminal) -->
      <template v-else-if="effectiveState === 'failed'">
        <rect x="16" y="12" width="28" height="24" class="ico-fail-bg" />
        <line x1="20" y1="16" x2="40" y2="32" class="ico-x" />
        <line x1="40" y1="16" x2="20" y2="32" class="ico-x" />
      </template>

      <!-- cancelled — same X, dimmer -->
      <template v-else-if="effectiveState === 'cancelled'">
        <line x1="20" y1="14" x2="40" y2="34" class="ico-x dim" />
        <line x1="40" y1="14" x2="20" y2="34" class="ico-x dim" />
      </template>

      <!-- idle / vacant — two dashes -->
      <template v-else>
        <text x="30" y="28" text-anchor="middle" class="ico-idle">--</text>
      </template>
    </svg>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { TaskState } from '../../types'

const props = defineProps<{ state: TaskState | null }>()

const effectiveState = computed<TaskState | 'idle'>(() => props.state || 'idle')
</script>

<style scoped>
.station-screen {
  flex: 1;
  background: #080400;
  border: 2px solid #1c1000;
  padding: 2px;
  display: flex;
  align-items: stretch;
  justify-content: stretch;
}
.screen-svg {
  width: 100%;
  height: 100%;
}

/* shared icon strokes / fills */
.ico-stroke { stroke: var(--color-teal); stroke-width: 2; }
.ico-grid   { stroke: rgba(0, 187, 170, 0.45); stroke-width: 1; }
.ico-accent { fill: var(--color-amber); }
.ico-idle {
  font-family: var(--font-pixel);
  font-size: 10px;
  fill: #3a2510;
}

/* working — animated scroll lines */
.ico-line { fill: var(--color-teal); opacity: 0.9; animation: lineScroll 2s linear infinite; }
.line1 { animation-delay: 0s; }
.line2 { fill: var(--color-sky);   opacity: 0.55; animation-delay: -0.5s; }
.line3 { fill: var(--color-green); opacity: 0.7;  animation-delay: -1.0s; }
.line4 { fill: var(--color-teal);  opacity: 0.4;  animation-delay: -1.5s; }
@keyframes lineScroll {
  0%, 100% { opacity: 0.85; }
  50%      { opacity: 0.3; }
}

/* planning */
.ico-bp-bg { fill: rgba(68, 170, 238, 0.18); stroke: var(--color-sky); stroke-width: 1; }

/* awaiting-review */
.ico-paper { fill: rgba(232, 170, 0, 0.1); stroke: var(--color-amber); stroke-width: 1; }
.ico-clip  { fill: var(--color-amber); }

/* followup */
.ico-note { stroke: rgba(0, 0, 0, 0.5); stroke-width: 1; }
.note-a { fill: var(--color-orange); opacity: 0.85; }
.note-b { fill: var(--color-amber);  opacity: 0.85; }
.note-c { fill: var(--color-gold);   opacity: 0.85; }

/* done — stepped check draw */
.ico-check {
  stroke: var(--color-green);
  stroke-width: 3;
  stroke-linecap: square;
  stroke-linejoin: miter;
  filter: drop-shadow(0 0 4px var(--color-green));
  stroke-dasharray: 60;
  stroke-dashoffset: 60;
  animation: drawCheck 1.4s ease-out forwards;
}
@keyframes drawCheck {
  to { stroke-dashoffset: 0; }
}

/* failed */
.ico-fail-bg { fill: rgba(238, 51, 34, 0.12); stroke: var(--color-red); stroke-width: 1; }
.ico-x {
  stroke: var(--color-red);
  stroke-width: 3;
  stroke-linecap: square;
  filter: drop-shadow(0 0 4px var(--color-red));
}
.ico-x.dim { stroke: rgba(238, 51, 34, 0.55); filter: none; }
</style>
