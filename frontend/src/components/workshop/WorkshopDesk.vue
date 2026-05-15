<template>
  <div
    class="desk-card"
    :class="[`desk-${deskState}`, { 'desk-selected': selected }]"
    @click="$emit('click')"
    @keydown.enter="$emit('click')"
    role="button"
    tabindex="0"
    :title="`${worker.name} — ${deskState}`"
  >
    <svg viewBox="0 0 160 175" class="desk-svg" xmlns="http://www.w3.org/2000/svg" overflow="visible">
      <defs>
        <radialGradient :id="`lamp-${uid}`" cx="50%" cy="40%" r="60%">
          <stop offset="0%" stop-color="#fff5cc" stop-opacity="0.38" />
          <stop offset="70%" stop-color="#ffe070" stop-opacity="0.12" />
          <stop offset="100%" stop-color="#ffe070" stop-opacity="0" />
        </radialGradient>
        <linearGradient :id="`wood-${uid}`" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#8b5e2a" />
          <stop offset="100%" stop-color="#5c3d1e" />
        </linearGradient>
        <linearGradient :id="`wood-dark-${uid}`" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#4a3020" />
          <stop offset="100%" stop-color="#2d1a08" />
        </linearGradient>
      </defs>

      <!-- Floor shadow -->
      <ellipse cx="80" cy="168" rx="56" ry="6" fill="rgba(0,0,0,0.32)" class="floor-shadow" />

      <!-- ═══ CHAIR (rendered first = behind desk) ═══ -->
      <g class="desk-chair">
        <!-- Top rail -->
        <rect x="42" y="132" width="76" height="9" rx="3" :fill="`url(#wood-dark-${uid})`" stroke="#3d2510" stroke-width="1" />
        <!-- Back slats -->
        <rect x="50" y="119" width="9" height="15" rx="2" :fill="`url(#wood-dark-${uid})`" stroke="#2d1a08" stroke-width="0.8" />
        <rect x="63" y="116" width="9" height="18" rx="2" :fill="`url(#wood-dark-${uid})`" stroke="#2d1a08" stroke-width="0.8" />
        <rect x="76" y="114" width="9" height="20" rx="2" :fill="`url(#wood-dark-${uid})`" stroke="#2d1a08" stroke-width="0.8" />
        <rect x="89" y="116" width="9" height="18" rx="2" :fill="`url(#wood-dark-${uid})`" stroke="#2d1a08" stroke-width="0.8" />
        <rect x="102" y="119" width="9" height="15" rx="2" :fill="`url(#wood-dark-${uid})`" stroke="#2d1a08" stroke-width="0.8" />
        <!-- Seat -->
        <rect x="40" y="138" width="80" height="18" rx="3" :fill="`url(#wood-${uid})`" stroke="#4a3020" stroke-width="1" />
        <!-- Seat cushion detail -->
        <rect x="44" y="140" width="72" height="14" rx="2" fill="#6b4420" stroke="none" opacity="0.5" />
        <!-- Front legs -->
        <rect x="44" y="155" width="8" height="14" rx="2" :fill="`url(#wood-dark-${uid})`" stroke="#2d1a08" stroke-width="0.8" />
        <rect x="108" y="155" width="8" height="14" rx="2" :fill="`url(#wood-dark-${uid})`" stroke="#2d1a08" stroke-width="0.8" />
      </g>

      <!-- ═══ DESK LEGS + STRETCHER ═══ -->
      <rect x="20" y="108" width="14" height="38" rx="2" :fill="`url(#wood-dark-${uid})`" stroke="#2d1a08" stroke-width="1" />
      <rect x="126" y="108" width="14" height="38" rx="2" :fill="`url(#wood-dark-${uid})`" stroke="#2d1a08" stroke-width="1" />
      <!-- Cross-stretcher -->
      <rect x="22" y="126" width="116" height="6" rx="2" fill="#2d1a08" stroke="#1a0d04" stroke-width="0.5" />

      <!-- ═══ DESK SURFACE ═══ -->
      <!-- Top -->
      <rect x="14" y="58" width="132" height="53" rx="3" :fill="`url(#wood-${uid})`" stroke="#4a3020" stroke-width="1.5" class="desk-top" />
      <!-- Wood grain lines -->
      <line x1="21" y1="64" x2="139" y2="64" stroke="#7a4f2e" stroke-width="0.7" opacity="0.45" />
      <line x1="21" y1="70" x2="139" y2="70" stroke="#7a4f2e" stroke-width="0.5" opacity="0.35" />
      <line x1="21" y1="76" x2="139" y2="76" stroke="#7a4f2e" stroke-width="0.7" opacity="0.4" />
      <line x1="21" y1="82" x2="139" y2="82" stroke="#7a4f2e" stroke-width="0.5" opacity="0.3" />
      <line x1="21" y1="88" x2="139" y2="88" stroke="#7a4f2e" stroke-width="0.7" opacity="0.38" />
      <line x1="21" y1="94" x2="139" y2="94" stroke="#7a4f2e" stroke-width="0.5" opacity="0.28" />
      <line x1="21" y1="100" x2="139" y2="100" stroke="#7a4f2e" stroke-width="0.6" opacity="0.35" />
      <line x1="21" y1="106" x2="139" y2="106" stroke="#7a4f2e" stroke-width="0.5" opacity="0.3" />
      <!-- Front face of desk -->
      <rect x="14" y="107" width="132" height="10" rx="2" fill="#3d2510" stroke="#2d1a08" stroke-width="1" />
      <!-- Drawer strip -->
      <rect x="46" y="109" width="68" height="7" rx="1" fill="#2d1a08" stroke="#3d2510" stroke-width="0.8" />
      <!-- Drawer handle (brass) -->
      <rect x="72" y="110" width="16" height="4" rx="1.5" fill="#a07800" stroke="#e8aa00" stroke-width="0.5" />

      <!-- ═══ LAMP (working / reviewing / followup) ═══ -->
      <g class="lamp-group" :class="{ 'lamp-on': showLamp }">
        <!-- Glow cone on desk surface -->
        <ellipse cx="74" cy="84" rx="36" ry="20" :fill="`url(#lamp-${uid})`" class="lamp-glow-cone" />
        <!-- Base -->
        <rect x="118" y="93" width="8" height="12" rx="2" fill="#a07800" />
        <!-- Arm -->
        <path d="M122 93 C128 80 127 66 116 58" stroke="#c8a000" stroke-width="2.5" fill="none" stroke-linecap="round" />
        <!-- Shade -->
        <ellipse cx="111" cy="56" rx="14" ry="6" class="lamp-shade" />
        <!-- Bulb dot -->
        <circle cx="111" cy="59" r="4" class="lamp-bulb" />
      </g>

      <!-- ═══ STATE ITEMS ═══ -->

      <!-- IDLE: books on left + coffee mug on right -->
      <g class="state-items idle-items" :class="{ active: deskState === 'idle' }">
        <!-- Book stack -->
        <rect x="22" y="77" width="24" height="28" rx="1" fill="#2d1a08" stroke="#5c3d1e" stroke-width="1" />
        <rect x="22" y="77" width="24" height="6" fill="#7a1500" />
        <rect x="22" y="84" width="24" height="6" fill="#1a4060" />
        <rect x="22" y="91" width="24" height="6" fill="#3a5010" />
        <rect x="22" y="98" width="24" height="6" fill="#502810" />
        <!-- Spine highlights -->
        <line x1="27" y1="78" x2="27" y2="104" stroke="#ffe8c0" stroke-width="0.5" opacity="0.2" />
        <!-- Mug body -->
        <rect x="110" y="72" width="20" height="22" rx="3" fill="#3d2510" stroke="#a07800" stroke-width="1.5" />
        <!-- Handle -->
        <path d="M130 77 Q139 77 139 83 Q139 89 130 89" stroke="#a07800" stroke-width="1.8" fill="none" />
        <!-- Liquid top -->
        <ellipse cx="120" cy="72" rx="10" ry="3" fill="#2d1500" />
        <!-- Steam wisps -->
        <path d="M115 70 Q112 62 115 55" stroke="#ffe8b0" stroke-width="1.1" fill="none" class="steam-wisp" opacity="0.5" />
        <path d="M122 70 Q125 62 122 55" stroke="#ffe8b0" stroke-width="1.1" fill="none" class="steam-wisp" style="animation-delay:0.45s" opacity="0.4" />
      </g>

      <!-- WORKING: laptop open (screen above desk) + papers -->
      <g class="state-items working-items" :class="{ active: deskState === 'working' }">
        <!-- Papers left -->
        <rect x="20" y="70" width="32" height="35" rx="1" fill="#eed990" stroke="#886644" stroke-width="0.8" />
        <rect x="22" y="72" width="30" height="33" rx="1" fill="#ffe8c0" stroke="#886644" stroke-width="0.5" transform="rotate(5 37 88)" />
        <line x1="26" y1="79" x2="46" y2="79" stroke="#886644" stroke-width="0.7" opacity="0.55" />
        <line x1="26" y1="83" x2="46" y2="83" stroke="#886644" stroke-width="0.7" opacity="0.5" />
        <line x1="26" y1="87" x2="46" y2="87" stroke="#886644" stroke-width="0.7" opacity="0.55" />
        <line x1="26" y1="91" x2="40" y2="91" stroke="#886644" stroke-width="0.7" opacity="0.4" />
        <!-- Laptop screen -->
        <rect x="57" y="32" width="54" height="36" rx="2" fill="#0d0700" stroke="#00bbaa" stroke-width="1.5" class="laptop-screen" />
        <!-- Screen glow -->
        <rect x="60" y="35" width="48" height="30" rx="1" class="screen-glow" />
        <!-- Code lines on screen -->
        <rect x="63" y="38" width="32" height="2" rx="0.5" fill="#00ddbb" opacity="0.75" />
        <rect x="63" y="42" width="22" height="2" rx="0.5" fill="#33dd88" opacity="0.6" />
        <rect x="63" y="46" width="40" height="2" rx="0.5" fill="#00bbaa" opacity="0.55" />
        <rect x="63" y="50" width="16" height="2" rx="0.5" fill="#33dd88" opacity="0.7" />
        <rect x="63" y="54" width="34" height="2" rx="0.5" fill="#00bbaa" opacity="0.45" />
        <rect x="67" y="58" width="26" height="2" rx="0.5" fill="#33dd88" opacity="0.55" />
        <!-- Laptop hinge -->
        <rect x="52" y="66" width="64" height="4" rx="1.5" fill="#1a0d04" stroke="#2d1a08" stroke-width="0.8" />
        <!-- Keyboard base -->
        <rect x="52" y="68" width="64" height="8" rx="2" fill="#2d1a08" stroke="#4a3020" stroke-width="1" />
        <!-- Trackpad -->
        <rect x="72" y="70" width="24" height="4" rx="1" fill="#1a0d04" stroke="#3d2510" stroke-width="0.5" />
      </g>

      <!-- REVIEWING: clipboard + magnifying glass -->
      <g class="state-items reviewing-items" :class="{ active: deskState === 'reviewing' }">
        <!-- Clipboard board -->
        <rect x="33" y="59" width="48" height="54" rx="2" fill="#4a3020" stroke="#886644" stroke-width="1.2" />
        <!-- Clip -->
        <rect x="49" y="56" width="16" height="9" rx="2" fill="#3d2510" stroke="#a07800" stroke-width="1" />
        <rect x="53" y="58" width="8" height="5" rx="1" fill="#1a0d04" />
        <!-- Paper -->
        <rect x="36" y="65" width="42" height="42" rx="1" fill="#ffe8c0" />
        <!-- Checklist -->
        <rect x="39" y="69" width="5" height="5" rx="0.5" fill="none" stroke="#5c3d1e" stroke-width="0.9" />
        <path d="M40 71.2 L41.5 73 L44.5 70" stroke="#33dd88" stroke-width="1.1" fill="none" />
        <line x1="47" y1="71.5" x2="74" y2="71.5" stroke="#886644" stroke-width="0.8" opacity="0.7" />
        <rect x="39" y="77" width="5" height="5" rx="0.5" fill="none" stroke="#5c3d1e" stroke-width="0.9" />
        <path d="M40 79.2 L41.5 81 L44.5 78" stroke="#33dd88" stroke-width="1.1" fill="none" />
        <line x1="47" y1="79.5" x2="74" y2="79.5" stroke="#886644" stroke-width="0.8" opacity="0.7" />
        <rect x="39" y="85" width="5" height="5" rx="0.5" fill="none" stroke="#5c3d1e" stroke-width="0.9" />
        <line x1="47" y1="87.5" x2="74" y2="87.5" stroke="#886644" stroke-width="0.8" opacity="0.7" />
        <rect x="39" y="93" width="5" height="5" rx="0.5" fill="none" stroke="#5c3d1e" stroke-width="0.9" />
        <line x1="47" y1="95.5" x2="68" y2="95.5" stroke="#886644" stroke-width="0.8" opacity="0.7" />
        <!-- Magnifying glass -->
        <circle cx="108" cy="76" r="15" fill="rgba(232,170,0,0.07)" stroke="#e8aa00" stroke-width="2" class="mag-glass" />
        <circle cx="108" cy="76" r="11" fill="rgba(68,170,238,0.06)" stroke="#e8aa00" stroke-width="0.8" />
        <!-- Handle -->
        <line x1="119" y1="88" x2="129" y2="100" stroke="#6b4420" stroke-width="4.5" stroke-linecap="round" />
        <line x1="119" y1="88" x2="129" y2="100" stroke="#e8aa00" stroke-width="1.5" stroke-linecap="round" />
      </g>

      <!-- FOLLOWUP: papers + sticky note + flag pin -->
      <g class="state-items followup-items" :class="{ active: deskState === 'followup' }">
        <!-- Papers -->
        <rect x="36" y="66" width="68" height="40" rx="1" fill="#eed990" stroke="#886644" stroke-width="0.8" />
        <rect x="38" y="68" width="64" height="36" rx="1" fill="#ffe8c0" stroke="#886644" stroke-width="0.5" />
        <line x1="42" y1="74" x2="94" y2="74" stroke="#886644" stroke-width="0.7" opacity="0.5" />
        <line x1="42" y1="78" x2="94" y2="78" stroke="#886644" stroke-width="0.7" opacity="0.5" />
        <line x1="42" y1="82" x2="90" y2="82" stroke="#886644" stroke-width="0.7" opacity="0.4" />
        <line x1="42" y1="86" x2="92" y2="86" stroke="#886644" stroke-width="0.7" opacity="0.5" />
        <line x1="42" y1="90" x2="82" y2="90" stroke="#886644" stroke-width="0.7" opacity="0.35" />
        <!-- Red annotation marks -->
        <line x1="42" y1="78" x2="64" y2="78" stroke="#ee3322" stroke-width="1.5" opacity="0.7" />
        <circle cx="42" cy="82" r="2.2" fill="none" stroke="#ee3322" stroke-width="1" opacity="0.6" />
        <!-- Sticky note (tilted) -->
        <rect
          x="105" y="59"
          width="32" height="28"
          rx="1"
          fill="#ffcc00"
          stroke="#a07800"
          stroke-width="1"
          transform="rotate(10 121 73)"
          class="sticky-note"
        />
        <line x1="109" y1="66" x2="131" y2="63" stroke="#886644" stroke-width="0.8" opacity="0.6" transform="rotate(10 120 64)" />
        <line x1="109" y1="71" x2="131" y2="68" stroke="#886644" stroke-width="0.8" opacity="0.6" transform="rotate(10 120 69)" />
        <line x1="109" y1="76" x2="129" y2="73" stroke="#886644" stroke-width="0.8" opacity="0.5" transform="rotate(10 119 74)" />
        <!-- Flag pin (top-left) -->
        <line x1="24" y1="46" x2="24" y2="80" stroke="#cc5500" stroke-width="2" stroke-linecap="round" />
        <polygon points="24,46 46,54 24,66" fill="#ff7700" stroke="#cc5500" stroke-width="0.8" class="flag" />
        <circle cx="24" cy="80" r="3.2" fill="#cc5500" />
      </g>

      <!-- OFFLINE: dust cover -->
      <g class="state-items offline-items" :class="{ active: deskState === 'offline' }">
        <rect x="16" y="58" width="128" height="55" rx="4" fill="rgba(18,9,0,0.78)" stroke="#2d1a08" stroke-width="1.5" class="dust-cover" />
        <path d="M26 70 Q62 66 98 70 Q124 73 138 70" stroke="#1a0d04" stroke-width="1.2" fill="none" opacity="0.75" />
        <path d="M20 82 Q56 78 92 82 Q122 86 140 82" stroke="#1a0d04" stroke-width="1" fill="none" opacity="0.65" />
        <path d="M24 96 Q60 92 96 96 Q124 100 138 96" stroke="#1a0d04" stroke-width="1" fill="none" opacity="0.55" />
        <!-- ZZZ -->
        <text x="60" y="93" fill="#3d2510" font-size="16" font-family="monospace" opacity="0.45" letter-spacing="4">zzz</text>
      </g>

      <!-- ERROR: papers scattered + warning triangle -->
      <g class="state-items error-items" :class="{ active: deskState === 'error' }">
        <rect x="22" y="68" width="28" height="34" rx="1" fill="#eed990" stroke="#886644" stroke-width="0.8" transform="rotate(-14 36 85)" />
        <rect x="108" y="66" width="26" height="32" rx="1" fill="#ffe8c0" stroke="#886644" stroke-width="0.5" transform="rotate(13 121 82)" />
        <!-- Warning triangle -->
        <polygon points="80,60 98,92 62,92" fill="none" stroke="#ee3322" stroke-width="2.5" class="error-tri" />
        <line x1="80" y1="67" x2="80" y2="80" stroke="#ee3322" stroke-width="2.2" stroke-linecap="round" />
        <circle cx="80" cy="86" r="2" fill="#ee3322" />
      </g>

      <!-- Desk glow overlay — colour changes via CSS per state -->
      <rect x="14" y="58" width="132" height="53" rx="3" class="desk-glow-rect" fill="none" />
    </svg>

    <!-- Text labels below the SVG -->
    <div class="desk-info">
      <div class="desk-worker-name">{{ workerDisplayName }}</div>
      <div v-if="taskName && deskState !== 'offline'" class="desk-task-name">
        {{ truncate(taskName, 24) }}
      </div>
      <div class="desk-state-pill" :class="`pill-${deskState}`">{{ deskState }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, getCurrentInstance } from 'vue'
import type { Worker } from '../../types'

type DeskState = 'idle' | 'working' | 'reviewing' | 'followup' | 'offline' | 'error'

const props = defineProps<{
  worker: Worker
  taskName?: string
  deskState: DeskState
  selected?: boolean
}>()

defineEmits<{ click: [] }>()

const instance = getCurrentInstance()
const uid = computed(() => `d${instance?.uid ?? Math.random().toString(36).slice(2, 8)}`)

const showLamp = computed(() => ['working', 'reviewing', 'followup'].includes(props.deskState))

const workerDisplayName = computed(() => {
  const name = props.worker.name || props.worker.id
  // Show last segment if it looks like "hostname/w-id"
  const slash = name.lastIndexOf('/')
  return slash !== -1 ? name.slice(slash + 1) : name
})

function truncate(text: string, max: number): string {
  return text.length > max ? text.slice(0, max - 1) + '…' : text
}
</script>

<style scoped>
/* ─── Wrapper ────────────────────────────────────────────── */
.desk-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  cursor: pointer;
  border-radius: 6px;
  padding: 8px 6px 6px;
  transition:
    background 0.2s,
    box-shadow 0.35s;
  outline: none;
  position: relative;
  user-select: none;
}

.desk-card:hover {
  background: rgba(232, 170, 0, 0.06);
}

.desk-card:focus-visible {
  outline: 2px solid var(--color-brass);
  outline-offset: 2px;
}

.desk-card.desk-selected {
  background: rgba(0, 187, 170, 0.08);
  box-shadow: 0 0 0 1px rgba(0, 187, 170, 0.3);
}

/* ─── SVG sizing ─────────────────────────────────────────── */
.desk-svg {
  width: 100%;
  max-width: 160px;
  height: auto;
  overflow: visible;
}

/* ─── State glow on card ─────────────────────────────────── */
.desk-card.desk-working {
  box-shadow: 0 0 18px rgba(0, 187, 170, 0.2);
}
.desk-card.desk-reviewing {
  box-shadow: 0 0 18px rgba(255, 204, 0, 0.22);
}
.desk-card.desk-followup {
  box-shadow: 0 0 18px rgba(255, 119, 0, 0.2);
}
.desk-card.desk-error {
  box-shadow: 0 0 18px rgba(238, 51, 34, 0.22);
}
.desk-card.desk-offline {
  opacity: 0.55;
}

/* ─── Desk top shade by state ────────────────────────────── */
.desk-glow-rect {
  transition: stroke 0.35s;
  stroke: transparent;
  stroke-width: 0;
}
.desk-card.desk-working .desk-glow-rect {
  stroke: rgba(0, 187, 170, 0.25);
  stroke-width: 2;
}
.desk-card.desk-reviewing .desk-glow-rect {
  stroke: rgba(255, 204, 0, 0.3);
  stroke-width: 2;
}
.desk-card.desk-followup .desk-glow-rect {
  stroke: rgba(255, 119, 0, 0.28);
  stroke-width: 2;
}
.desk-card.desk-error .desk-glow-rect {
  stroke: rgba(238, 51, 34, 0.28);
  stroke-width: 2;
}

/* ─── State items fade transitions ───────────────────────── */
.state-items {
  opacity: 0;
  transition: opacity 0.4s ease;
  pointer-events: none;
}
.state-items.active {
  opacity: 1;
}

/* ─── Lamp ───────────────────────────────────────────────── */
.lamp-group {
  opacity: 0;
  transition: opacity 0.35s ease;
}
.lamp-group.lamp-on {
  opacity: 1;
}
.lamp-shade {
  fill: #e8aa00;
  transition: fill 0.35s;
}
.lamp-bulb {
  fill: #fff5cc;
  filter: drop-shadow(0 0 4px #ffe070);
  transition: fill 0.35s;
}
.desk-card.desk-reviewing .lamp-shade {
  fill: #ffcc00;
}
.desk-card.desk-followup .lamp-shade {
  fill: #ff9900;
}
.lamp-glow-cone {
  transition: opacity 0.35s;
}

/* Laptop screen glow */
.laptop-screen {
  filter: drop-shadow(0 0 6px rgba(0, 187, 170, 0.4));
}
.screen-glow {
  fill: rgba(0, 80, 60, 0.55);
  animation: screenFlicker 3.5s ease-in-out infinite;
}
@keyframes screenFlicker {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.82;
  }
}

/* Mag glass shimmer */
.mag-glass {
  animation: magSpin 6s linear infinite;
  transform-origin: 108px 76px;
}
@keyframes magSpin {
  0% {
    stroke-dashoffset: 0;
  }
  100% {
    stroke-dashoffset: 0;
  }
}
/* subtle pulse on reviewing */
.desk-card.desk-reviewing .mag-glass {
  animation: magPulse 2s ease-in-out infinite;
}
@keyframes magPulse {
  0%,
  100% {
    stroke-opacity: 1;
  }
  50% {
    stroke-opacity: 0.5;
  }
}

/* Sticky note wobble */
.desk-card.desk-followup .sticky-note {
  animation: noteWobble 4s ease-in-out infinite;
  transform-origin: 121px 73px;
}
@keyframes noteWobble {
  0%,
  100% {
    transform: rotate(10deg);
  }
  50% {
    transform: rotate(7deg);
  }
}

/* Flag wave */
.desk-card.desk-followup .flag {
  animation: flagWave 2s ease-in-out infinite;
  transform-origin: 24px 46px;
}
@keyframes flagWave {
  0%,
  100% {
    transform: skewX(0deg);
  }
  50% {
    transform: skewX(-6deg);
  }
}

/* Steam wisps on mug */
.steam-wisp {
  animation: steamRise 2.5s ease-in-out infinite;
  transform-origin: center;
}
@keyframes steamRise {
  0% {
    opacity: 0;
    transform: translateY(4px);
  }
  40% {
    opacity: 0.55;
  }
  100% {
    opacity: 0;
    transform: translateY(-6px);
  }
}

/* Working shimmer on desk top */
.desk-card.desk-working .desk-top {
  animation: warmShimmer 4s ease-in-out infinite;
}
@keyframes warmShimmer {
  0%,
  100% {
    filter: brightness(1);
  }
  50% {
    filter: brightness(1.08);
  }
}

/* Error triangle pulse */
.desk-card.desk-error .error-tri {
  animation: errPulse 1s ease-in-out infinite;
}
@keyframes errPulse {
  0%,
  100% {
    stroke-opacity: 1;
  }
  50% {
    stroke-opacity: 0.4;
  }
}

/* Chair: tucked under for offline */
.desk-chair {
  transition: transform 0.5s ease;
}
.desk-card.desk-offline .desk-chair {
  transform: translateY(-30px);
}

/* ─── Info labels ─────────────────────────────────────────── */
.desk-info {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 3px;
  margin-top: 4px;
  max-width: 148px;
  text-align: center;
}

.desk-worker-name {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-brass-light);
  letter-spacing: 0.5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 140px;
}

.desk-task-name {
  font-family: var(--font-mono);
  font-size: 9px;
  color: var(--color-text-dim);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 140px;
  line-height: 1.2;
}

.desk-state-pill {
  font-family: var(--font-pixel);
  font-size: 5px;
  padding: 2px 5px;
  border-radius: 2px;
  border: 1px solid;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  transition:
    color 0.3s,
    border-color 0.3s,
    background 0.3s;
}

.pill-idle {
  color: var(--color-text-dim);
  border-color: rgba(136, 102, 68, 0.4);
  background: rgba(136, 102, 68, 0.08);
}
.pill-working {
  color: var(--color-green);
  border-color: rgba(51, 221, 136, 0.4);
  background: rgba(51, 221, 136, 0.08);
}
.pill-reviewing {
  color: var(--color-amber);
  border-color: rgba(255, 204, 0, 0.4);
  background: rgba(255, 204, 0, 0.08);
}
.pill-followup {
  color: var(--color-orange);
  border-color: rgba(255, 119, 0, 0.4);
  background: rgba(255, 119, 0, 0.08);
}
.pill-offline {
  color: #3d2510;
  border-color: rgba(45, 26, 8, 0.4);
  background: transparent;
}
.pill-error {
  color: var(--color-red);
  border-color: rgba(238, 51, 34, 0.4);
  background: rgba(238, 51, 34, 0.08);
}
</style>
