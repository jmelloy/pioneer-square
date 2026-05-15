<template>
  <div
    class="work-table"
    :class="[`state-${displayState}`, { occupied: !!task, clickable: !!task }]"
    @click="task && $emit('click', task)"
    :title="task ? (task.name || task.id) : ''"
  >
    <svg
      viewBox="0 0 200 148"
      xmlns="http://www.w3.org/2000/svg"
      class="table-svg"
      preserveAspectRatio="xMidYMid meet"
    >
      <!-- ── Table structure ────────────────────────────────── -->
      <!-- Surface -->
      <rect x="4" y="24" width="192" height="80" rx="2" class="surface" />
      <!-- Wood grain -->
      <line x1="14" y1="36" x2="186" y2="36" class="grain" />
      <line x1="14" y1="52" x2="186" y2="52" class="grain" />
      <line x1="14" y1="68" x2="186" y2="68" class="grain" />
      <line x1="14" y1="84" x2="186" y2="84" class="grain" />
      <!-- Front edge (3-D depth) -->
      <rect x="4" y="104" width="192" height="10" rx="1" class="edge" />
      <!-- Legs -->
      <rect x="12" y="114" width="10" height="26" rx="2" class="leg" />
      <rect x="178" y="114" width="10" height="26" rx="2" class="leg" />
      <rect x="42" y="114" width="7" height="20" rx="1" class="leg" />
      <rect x="151" y="114" width="7" height="20" rx="1" class="leg" />
      <!-- Rail connecting legs -->
      <rect x="12" y="130" width="176" height="4" rx="1" class="rail" />

      <!-- ── State overlays ─────────────────────────────────── -->

      <!-- IDLE -->
      <template v-if="displayState === 'idle'">
        <text x="100" y="74" text-anchor="middle" class="idle-label">— vacant —</text>
      </template>

      <!-- PLANNING / plan -->
      <template v-else-if="displayState === 'planning'">
        <!-- Blueprint sheet -->
        <rect x="28" y="30" width="88" height="62" rx="2" class="blueprint-bg" />
        <rect x="28" y="30" width="88" height="10" rx="2" class="blueprint-hdr" />
        <!-- Grid lines -->
        <line x1="36" y1="46" x2="108" y2="46" class="bp-grid" />
        <line x1="36" y1="54" x2="108" y2="54" class="bp-grid" />
        <line x1="36" y1="62" x2="108" y2="62" class="bp-grid" />
        <line x1="36" y1="70" x2="108" y2="70" class="bp-grid" />
        <line x1="56" y1="40" x2="56" y2="80" class="bp-grid" />
        <line x1="80" y1="40" x2="80" y2="80" class="bp-grid" />
        <line x1="100" y1="40" x2="100" y2="80" class="bp-grid" />
        <!-- Pencil -->
        <rect x="120" y="34" width="5" height="36" rx="1.5" class="pencil-body" transform="rotate(20,122,52)" />
        <polygon points="120,70 125,70 122.5,77" class="pencil-tip" transform="rotate(20,122,52)" />
        <rect x="120" y="34" width="5" height="5" rx="1" class="pencil-eraser" transform="rotate(20,122,52)" />
        <!-- Ruler -->
        <rect x="130" y="52" width="44" height="8" rx="1" class="ruler" transform="rotate(-12,152,56)" />
        <line x1="136" y1="52" x2="136" y2="60" class="ruler-tick" transform="rotate(-12,152,56)" />
        <line x1="144" y1="52" x2="144" y2="60" class="ruler-tick" transform="rotate(-12,152,56)" />
        <line x1="152" y1="52" x2="152" y2="60" class="ruler-tick" transform="rotate(-12,152,56)" />
        <line x1="160" y1="52" x2="160" y2="60" class="ruler-tick" transform="rotate(-12,152,56)" />
        <line x1="168" y1="52" x2="168" y2="60" class="ruler-tick" transform="rotate(-12,152,56)" />
        <!-- Ambient glow -->
        <rect x="4" y="24" width="192" height="80" rx="2" class="plan-glow" />
      </template>

      <!-- WORKING / execute -->
      <template v-else-if="displayState === 'working'">
        <!-- Laptop screen -->
        <rect x="54" y="28" width="80" height="52" rx="3" class="laptop-screen-shell" />
        <rect x="58" y="31" width="72" height="45" rx="1" class="laptop-screen" />
        <!-- Code lines on screen -->
        <rect x="62" y="35" width="42" height="3" rx="1" class="code-ln c1" />
        <rect x="62" y="40" width="56" height="3" rx="1" class="code-ln c2" />
        <rect x="66" y="45" width="30" height="3" rx="1" class="code-ln c3" />
        <rect x="62" y="50" width="50" height="3" rx="1" class="code-ln c4" />
        <rect x="66" y="55" width="38" height="3" rx="1" class="code-ln c5" />
        <rect x="62" y="60" width="46" height="3" rx="1" class="code-ln c6" />
        <!-- Cursor blink -->
        <rect x="62" y="65" width="4" height="6" rx="0.5" class="cursor" />
        <!-- Laptop base/hinge -->
        <rect x="50" y="79" width="88" height="5" rx="1" class="hinge" />
        <rect x="46" y="84" width="96" height="14" rx="2" class="laptop-base" />
        <!-- Keyboard rows -->
        <rect x="52" y="87" width="84" height="3" rx="1" class="kbd-row" />
        <rect x="52" y="92" width="84" height="3" rx="1" class="kbd-row" />
        <!-- Coffee cup right side -->
        <rect x="150" y="70" width="18" height="26" rx="3" class="cup" />
        <path d="M168,77 Q176,77 176,83 Q176,89 168,89" class="cup-handle" fill="none" stroke-width="3" stroke-linecap="round" />
        <rect x="150" y="70" width="18" height="6" rx="3" class="cup-top" />
        <!-- Ambient glow -->
        <rect x="4" y="24" width="192" height="80" rx="2" class="work-glow" />
      </template>

      <!-- AWAITING-REVIEW -->
      <template v-else-if="displayState === 'awaiting-review'">
        <!-- Stacked paper pile -->
        <rect x="24" y="82" width="100" height="6" rx="1" class="paper-s3" />
        <rect x="22" y="74" width="102" height="10" rx="1" class="paper-s2" />
        <rect x="20" y="34" width="102" height="42" rx="2" class="paper-s1" />
        <!-- Lines on top page -->
        <rect x="28" y="40" width="82" height="3" rx="1" class="doc-line" />
        <rect x="28" y="47" width="76" height="3" rx="1" class="doc-line" />
        <rect x="28" y="54" width="82" height="3" rx="1" class="doc-line" />
        <rect x="28" y="61" width="60" height="3" rx="1" class="doc-line" />
        <rect x="28" y="68" width="70" height="3" rx="1" class="doc-line" />
        <!-- Desk lamp -->
        <rect x="140" y="86" width="30" height="6" rx="2" class="lamp-base" />
        <rect x="153" y="38" width="6" height="48" rx="2" class="lamp-pole" />
        <!-- Lamp shade -->
        <path d="M136,40 L174,40 L166,56 L144,56 Z" class="lamp-shade" />
        <!-- Light cone -->
        <path d="M144,56 L166,56 L174,90 L136,90 Z" class="lamp-light" />
        <!-- Ambient -->
        <rect x="4" y="24" width="192" height="80" rx="2" class="review-glow" />
      </template>

      <!-- FOLLOWUP -->
      <template v-else-if="displayState === 'followup'">
        <!-- Three sticky notes at angles -->
        <rect x="18" y="30" width="52" height="44" rx="2" class="sticky-a" />
        <line x1="24" y1="40" x2="64" y2="40" class="sticky-line-a" />
        <line x1="24" y1="47" x2="60" y2="47" class="sticky-line-a" />
        <line x1="24" y1="54" x2="62" y2="54" class="sticky-line-a" />
        <line x1="24" y1="61" x2="56" y2="61" class="sticky-line-a" />

        <rect x="76" y="34" width="50" height="42" rx="2" class="sticky-b" transform="rotate(-6,101,55)" />
        <line x1="82" y1="44" x2="120" y2="44" class="sticky-line-b" transform="rotate(-6,101,55)" />
        <line x1="82" y1="51" x2="116" y2="51" class="sticky-line-b" transform="rotate(-6,101,55)" />
        <line x1="82" y1="58" x2="118" y2="58" class="sticky-line-b" transform="rotate(-6,101,55)" />

        <rect x="134" y="28" width="50" height="46" rx="2" class="sticky-c" transform="rotate(8,159,51)" />
        <line x1="140" y1="38" x2="178" y2="38" class="sticky-line-c" transform="rotate(8,159,51)" />
        <line x1="140" y1="45" x2="174" y2="45" class="sticky-line-c" transform="rotate(8,159,51)" />
        <line x1="140" y1="52" x2="176" y2="52" class="sticky-line-c" transform="rotate(8,159,51)" />
        <!-- Ambient -->
        <rect x="4" y="24" width="192" height="80" rx="2" class="followup-glow" />
      </template>

      <!-- DONE -->
      <template v-else-if="displayState === 'done'">
        <!-- Clear surface, faint grain -->
        <!-- Big checkmark -->
        <path d="M38,72 L72,98 L162,38" class="checkmark" fill="none" stroke-width="12" stroke-linecap="round" stroke-linejoin="round" />
        <!-- Sparkles around checkmark -->
        <circle cx="164" cy="36" r="4" class="sparkle" />
        <circle cx="36" cy="70" r="3" class="sparkle" />
        <circle cx="100" cy="30" r="3.5" class="sparkle" />
        <!-- Ambient -->
        <rect x="4" y="24" width="192" height="80" rx="2" class="done-glow" />
      </template>

      <!-- FAILED / CANCELLED -->
      <template v-else-if="displayState === 'failed'">
        <!-- Scattered crumpled papers -->
        <rect x="14" y="30" width="60" height="46" rx="2" class="scattered-p" transform="rotate(-22,44,53)" />
        <rect x="90" y="58" width="56" height="38" rx="2" class="scattered-p" transform="rotate(16,118,77)" />
        <rect x="50" y="40" width="80" height="52" rx="2" class="scattered-p2" />
        <!-- X mark -->
        <line x1="46" y1="36" x2="154" y2="96" class="x-mark" stroke-width="14" stroke-linecap="round" />
        <line x1="154" y1="36" x2="46" y2="96" class="x-mark" stroke-width="14" stroke-linecap="round" />
        <!-- Ambient -->
        <rect x="4" y="24" width="192" height="80" rx="2" class="fail-glow" />
      </template>

      <!-- ── Header strip ──────────────────────────────────── -->
      <rect x="2" y="2" width="196" height="20" rx="2" class="header-bg" />
      <!-- Task name (clipped via unique clipPath per instance) -->
      <defs>
        <clipPath :id="`nc-${index}`">
          <rect x="4" y="2" width="134" height="20" />
        </clipPath>
      </defs>
      <text x="8" y="15" class="task-name" :clip-path="`url(#nc-${index})`">
        {{ task ? truncate(task.name || task.id, 22) : '— vacant bench —' }}
      </text>
      <!-- State badge -->
      <rect v-if="task" x="142" y="4" width="52" height="16" rx="2" :class="`badge-bg badge-${displayState}`" />
      <text v-if="task" x="168" y="15" text-anchor="middle" class="badge-txt" :fill="badgeColor">
        {{ stateLabel(task!.state) }}
      </text>
    </svg>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { Task } from '../../types'

const props = defineProps<{
  task: Task | null
  index: number
  stateLabel: (state: string) => string
}>()

defineEmits<{ click: [task: Task] }>()

const displayState = computed(() => {
  if (!props.task) return 'idle'
  const s = props.task.state
  if (s === 'planning') return 'planning'
  if (s === 'working' || s === 'pending') return 'working'
  if (s === 'awaiting-review') return 'awaiting-review'
  if (s === 'followup') return 'followup'
  if (s === 'done') return 'done'
  if (s === 'failed' || s === 'cancelled') return 'failed'
  return 'idle'
})

const BADGE_COLORS: Record<string, string> = {
  planning: '#44aaee',
  working: '#88dd22',
  'awaiting-review': '#ffcc00',
  followup: '#ff7700',
  done: '#00bbaa',
  failed: '#ee3322',
  idle: '#554433',
}

const badgeColor = computed(() => BADGE_COLORS[displayState.value] ?? '#554433')

function truncate(str: string, len: number) {
  return str.length > len ? str.slice(0, len) + '…' : str
}
</script>

<style scoped>
.work-table {
  position: relative;
  border-radius: 4px;
  transition:
    box-shadow 0.35s,
    transform 0.2s;
  user-select: none;
}

.work-table.clickable {
  cursor: pointer;
}

.work-table.clickable:hover {
  transform: translateY(-2px);
}

.table-svg {
  width: 100%;
  height: 100%;
  display: block;
  overflow: visible;
}

/* ── Table structure ───────────────────────────────────────── */
.surface {
  fill: #5c3818;
}
.grain {
  stroke: #6b4422;
  stroke-width: 0.8;
  opacity: 0.55;
}
.edge {
  fill: #3a1e08;
}
.leg {
  fill: #2e1606;
}
.rail {
  fill: #2e1606;
  opacity: 0.7;
}

/* ── Header ────────────────────────────────────────────────── */
.header-bg {
  fill: rgba(10, 5, 0, 0.88);
  stroke: rgba(232, 170, 0, 0.35);
  stroke-width: 0.75;
}
.task-name {
  font-family: var(--font-pixel, monospace);
  font-size: 7px;
  fill: #ffe8c0;
  letter-spacing: 0.8px;
}
.work-table:not(.occupied) .task-name {
  fill: #554433;
  font-style: italic;
}
.badge-bg {
  fill: rgba(0, 0, 0, 0.55);
  stroke-width: 0.75;
}
.badge-txt {
  font-family: var(--font-pixel, monospace);
  font-size: 5.5px;
  letter-spacing: 0.6px;
  text-transform: uppercase;
}
.badge-planning { stroke: #44aaee; fill: rgba(68,170,238,0.15); }
.badge-planning + .badge-txt, .badge-planning ~ .badge-txt { fill: #44aaee; }
.badge-working { stroke: #88dd22; fill: rgba(136,221,34,0.15); }
.badge-awaiting-review { stroke: #ffcc00; fill: rgba(255,204,0,0.12); }
.badge-followup { stroke: #ff7700; fill: rgba(255,119,0,0.15); }
.badge-done { stroke: #00bbaa; fill: rgba(0,187,170,0.12); }
.badge-failed { stroke: #ee3322; fill: rgba(238,51,34,0.15); }
.badge-idle { stroke: #554433; fill: transparent; }

/* ── IDLE ───────────────────────────────────────────────────── */
.idle-label {
  font-family: var(--font-pixel, monospace);
  font-size: 8px;
  fill: #4a3020;
  letter-spacing: 1px;
  font-style: italic;
}

/* ── PLAN ───────────────────────────────────────────────────── */
.blueprint-bg {
  fill: #0c1f40;
  stroke: #3366aa;
  stroke-width: 1;
}
.blueprint-hdr {
  fill: #1a3a6e;
  stroke: #3366aa;
  stroke-width: 0.5;
}
.bp-grid {
  stroke: #4488bb;
  stroke-width: 0.6;
  opacity: 0.65;
}
.pencil-body { fill: #eedd55; }
.pencil-tip { fill: #dd9944; }
.pencil-eraser { fill: #dd6655; }
.ruler {
  fill: #c8b88a;
  stroke: #886644;
  stroke-width: 0.5;
}
.ruler-tick {
  stroke: #886644;
  stroke-width: 0.6;
}
.plan-glow {
  fill: rgba(68, 136, 255, 0.07);
  pointer-events: none;
}

/* ── WORKING ────────────────────────────────────────────────── */
.laptop-screen-shell {
  fill: #1a1a1a;
  stroke: #444;
  stroke-width: 1;
}
.laptop-screen {
  fill: #0a1a0a;
  stroke: #223322;
  stroke-width: 0.5;
}
.code-ln { fill: #33aa44; opacity: 0.85; }
.c1 { fill: #33aa44; }
.c2 { fill: #88dd22; }
.c3 { fill: #44aadd; }
.c4 { fill: #33aa44; }
.c5 { fill: #ffaa22; opacity: 0.7; }
.c6 { fill: #88dd22; }
.cursor {
  fill: #88dd22;
  animation: cursorBlink 0.9s step-end infinite;
}
@keyframes cursorBlink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0; }
}
.hinge { fill: #333; }
.laptop-base {
  fill: #222;
  stroke: #444;
  stroke-width: 0.75;
}
.kbd-row { fill: #333; opacity: 0.9; }
.cup {
  fill: #8b4513;
  stroke: #6b3010;
  stroke-width: 0.75;
}
.cup-handle { stroke: #6b3010; }
.cup-top { fill: #5c2b08; }
.work-glow {
  fill: rgba(255, 136, 0, 0.06);
  pointer-events: none;
}

/* ── AWAITING-REVIEW ────────────────────────────────────────── */
.paper-s3 { fill: #c8b89a; stroke: #aa9a7a; stroke-width: 0.5; }
.paper-s2 { fill: #d4c4a4; stroke: #aa9a7a; stroke-width: 0.5; }
.paper-s1 {
  fill: #e4d8bc;
  stroke: #aa9a7a;
  stroke-width: 0.75;
}
.doc-line { fill: #8a7a62; opacity: 0.6; }
.lamp-base { fill: #888; stroke: #666; stroke-width: 0.75; }
.lamp-pole { fill: #aaa; stroke: #888; stroke-width: 0.5; }
.lamp-shade { fill: #cc9922; stroke: #aa7700; stroke-width: 0.75; }
.lamp-light {
  fill: rgba(255, 220, 100, 0.18);
  pointer-events: none;
}
.review-glow {
  fill: rgba(255, 204, 0, 0.07);
  pointer-events: none;
}

/* ── FOLLOWUP ───────────────────────────────────────────────── */
.sticky-a { fill: #ffee55; stroke: #ddc922; stroke-width: 0.75; }
.sticky-b { fill: #ff9944; stroke: #dd7722; stroke-width: 0.75; }
.sticky-c { fill: #55ddbb; stroke: #33aa99; stroke-width: 0.75; }
.sticky-line-a { stroke: #bb9900; stroke-width: 1; opacity: 0.55; }
.sticky-line-b { stroke: #aa5500; stroke-width: 1; opacity: 0.55; }
.sticky-line-c { stroke: #228877; stroke-width: 1; opacity: 0.55; }
.followup-glow {
  fill: rgba(255, 119, 0, 0.08);
  pointer-events: none;
}

/* ── DONE ───────────────────────────────────────────────────── */
.checkmark {
  stroke: #88dd22;
  stroke-dasharray: 350;
  stroke-dashoffset: 0;
  filter: drop-shadow(0 0 6px rgba(136, 221, 34, 0.8));
  animation: checkDraw 0.7s ease-out both;
}
@keyframes checkDraw {
  from { stroke-dashoffset: 350; }
  to { stroke-dashoffset: 0; }
}
.sparkle {
  fill: #88dd22;
  opacity: 0.75;
  animation: sparklePulse 1.8s ease-in-out infinite;
}
.sparkle:nth-child(2) { animation-delay: 0.4s; }
.sparkle:nth-child(3) { animation-delay: 0.8s; }
@keyframes sparklePulse {
  0%, 100% { opacity: 0.75; r: 4; }
  50% { opacity: 0.3; r: 2; }
}
.done-glow {
  fill: rgba(64, 220, 64, 0.08);
  pointer-events: none;
}

/* ── FAILED ─────────────────────────────────────────────────── */
.scattered-p {
  fill: #d4c4a4;
  stroke: #aa9a7a;
  stroke-width: 0.5;
  opacity: 0.7;
}
.scattered-p2 {
  fill: #e4d8bc;
  stroke: #aa9a7a;
  stroke-width: 0.75;
  opacity: 0.85;
}
.x-mark {
  stroke: #ee3322;
  filter: drop-shadow(0 0 5px rgba(238, 51, 34, 0.8));
}
.fail-glow {
  fill: rgba(220, 50, 50, 0.1);
  pointer-events: none;
}

/* ── State border glow on the wrapper ──────────────────────── */
.work-table.state-planning {
  box-shadow: 0 0 12px rgba(68, 136, 255, 0.35), 0 0 3px rgba(68, 136, 255, 0.5);
}
.work-table.state-working {
  box-shadow: 0 0 12px rgba(255, 136, 0, 0.35), 0 0 3px rgba(255, 136, 0, 0.5);
}
.work-table.state-awaiting-review {
  box-shadow: 0 0 12px rgba(255, 204, 0, 0.35), 0 0 3px rgba(255, 204, 0, 0.5);
}
.work-table.state-followup {
  box-shadow: 0 0 12px rgba(255, 119, 0, 0.35), 0 0 3px rgba(255, 119, 0, 0.5);
}
.work-table.state-done {
  box-shadow: 0 0 12px rgba(64, 220, 64, 0.3), 0 0 3px rgba(64, 220, 64, 0.4);
}
.work-table.state-failed {
  box-shadow: 0 0 12px rgba(220, 50, 50, 0.35), 0 0 3px rgba(220, 50, 50, 0.5);
}
.work-table.state-idle {
  box-shadow: 0 0 4px rgba(0, 0, 0, 0.4);
  opacity: 0.55;
}
</style>
