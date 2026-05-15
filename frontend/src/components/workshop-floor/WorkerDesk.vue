<template>
  <div
    class="worker-desk"
    :class="[`state-${deskState}`, { selected }]"
    @click="$emit('click')"
    :title="`${worker.name} — ${worker.state}${task ? ' | ' + (task.name || task.id) : ''}`"
  >
    <svg class="desk-svg" viewBox="0 0 140 140" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <!-- Warm wood grain pattern -->
        <pattern :id="`wg-${uid}`" patternUnits="userSpaceOnUse" width="30" height="30" patternTransform="rotate(2)">
          <rect width="30" height="30" fill="#7a4e28"/>
          <line x1="0" y1="5" x2="30" y2="6" stroke="#8e5e38" stroke-width="1.2" opacity="0.5"/>
          <line x1="0" y1="11" x2="30" y2="12" stroke="#6a3e18" stroke-width="0.8" opacity="0.45"/>
          <line x1="0" y1="18" x2="30" y2="19" stroke="#8e5e38" stroke-width="1" opacity="0.4"/>
          <line x1="0" y1="24" x2="30" y2="25.5" stroke="#6a3e18" stroke-width="0.7" opacity="0.5"/>
        </pattern>
        <!-- Dark wood grain (offline) -->
        <pattern :id="`dw-${uid}`" patternUnits="userSpaceOnUse" width="30" height="30" patternTransform="rotate(2)">
          <rect width="30" height="30" fill="#1c1008"/>
          <line x1="0" y1="8" x2="30" y2="9" stroke="#2a1810" stroke-width="1" opacity="0.6"/>
          <line x1="0" y1="17" x2="30" y2="18" stroke="#140c06" stroke-width="0.8" opacity="0.5"/>
          <line x1="0" y1="25" x2="30" y2="26" stroke="#2a1810" stroke-width="0.9" opacity="0.5"/>
        </pattern>
      </defs>

      <!-- ═══════════════════ OFFLINE ═══════════════════ -->
      <template v-if="deskState === 'offline'">
        <!-- Chair pushed under desk -->
        <rect x="49" y="16" width="42" height="6" rx="3" fill="#241608" opacity="0.55"/>
        <rect x="53" y="7" width="4" height="11" rx="1.5" fill="#1c1008" opacity="0.5"/>
        <rect x="83" y="7" width="4" height="11" rx="1.5" fill="#1c1008" opacity="0.5"/>
        <!-- Desk surface (dark / cold) -->
        <rect x="8" y="36" width="124" height="66" rx="2" :fill="`url(#dw-${uid})`" stroke="#120800" stroke-width="1.5"/>
        <!-- Desk front edge -->
        <rect x="8" y="100" width="124" height="12" rx="1" fill="#0e0604"/>
        <!-- Legs -->
        <rect x="12" y="110" width="8" height="24" rx="1" fill="#0a0402"/>
        <rect x="120" y="110" width="8" height="24" rx="1" fill="#0a0402"/>
      </template>

      <!-- ═══════════════════ ACTIVE STATES ═══════════════════ -->
      <template v-else>
        <!-- Desk surface -->
        <rect x="8" y="36" width="124" height="66" rx="2" :fill="`url(#wg-${uid})`" stroke="#5a3010" stroke-width="1.5"/>

        <!-- Lamp post (right-back corner) -->
        <rect x="124" y="42" width="3" height="42" rx="1" fill="#B8880A"/>
        <!-- Lamp arm -->
        <line x1="125.5" y1="56" x2="110" y2="48" stroke="#B8880A" stroke-width="2" stroke-linecap="round"/>
        <!-- Lamp shade -->
        <ellipse cx="106" cy="46" rx="11" ry="5" class="lamp-shade" :style="{ fill: lampShadeColor }"/>
        <ellipse cx="106" cy="46" rx="8" ry="3" fill="#0a0600" opacity="0.4"/>
        <!-- Lamp glow (active states only) -->
        <circle
          v-if="lampActive"
          cx="106" cy="56" r="26"
          class="lamp-glow"
          :style="{ fill: lampGlowColor }"
        />

        <!-- ── IDLE: clean desk — mug, pencil, notebook ── -->
        <Transition name="desk-fade" mode="out-in">
          <g v-if="deskState === 'idle'" key="idle">
            <!-- Coffee mug -->
            <ellipse cx="32" cy="71" rx="9" ry="3.5" fill="#3a1a08" stroke="#2a1002" stroke-width="0.8"/>
            <rect x="23" y="71" width="18" height="20" rx="2" fill="#6a3018" stroke="#4a1e08" stroke-width="1"/>
            <ellipse cx="32" cy="71" rx="7" ry="2.5" fill="#502808"/>
            <!-- Mug handle -->
            <path d="M41,74 Q48,74 48,81 Q48,88 41,88" stroke="#4a1e08" stroke-width="1.8" fill="none" stroke-linecap="round"/>
            <!-- Steam wisps -->
            <path d="M28,68 Q30,64 28,60" stroke="#aaa090" stroke-width="0.8" fill="none" opacity="0.4"/>
            <path d="M33,67 Q35,63 33,59" stroke="#aaa090" stroke-width="0.8" fill="none" opacity="0.3"/>

            <!-- Pencil (center, angled) -->
            <g transform="translate(76, 76) rotate(-22)">
              <rect x="-2.5" y="-24" width="5" height="40" fill="#F5C542"/>
              <rect x="-2.5" y="-24" width="5" height="7" rx="1" fill="#E89090"/>
              <rect x="-2.5" y="-17" width="5" height="2" fill="#888"/>
              <polygon points="-2.5,16 2.5,16 0,23" fill="#D4A84B"/>
              <rect x="-1.2" y="-14" width="2.4" height="28" fill="#E8B530" opacity="0.3"/>
            </g>

            <!-- Notebook -->
            <rect x="92" y="51" width="30" height="40" rx="1" fill="#EADECA" stroke="#C4A888" stroke-width="0.8"/>
            <rect x="92" y="51" width="5" height="40" rx="1" fill="#CC4444"/>
            <line x1="101" y1="59" x2="119" y2="59" stroke="#A09070" stroke-width="0.7" opacity="0.8"/>
            <line x1="101" y1="65" x2="119" y2="65" stroke="#A09070" stroke-width="0.7" opacity="0.8"/>
            <line x1="101" y1="71" x2="116" y2="71" stroke="#A09070" stroke-width="0.7" opacity="0.7"/>
            <line x1="101" y1="77" x2="119" y2="77" stroke="#A09070" stroke-width="0.7" opacity="0.6"/>
            <line x1="101" y1="83" x2="113" y2="83" stroke="#A09070" stroke-width="0.7" opacity="0.7"/>
          </g>

          <!-- ── WORKING: laptop + papers ── -->
          <g v-else-if="deskState === 'working'" key="working">
            <!-- Laptop screen -->
            <rect x="34" y="42" width="70" height="46" rx="3" fill="#18182A" stroke="#242436" stroke-width="1.5"/>
            <rect x="36" y="44" width="66" height="40" rx="1" fill="#081428" opacity="0.95"/>
            <!-- Code lines on screen -->
            <line x1="40" y1="50" x2="78" y2="50" stroke="#4499DD" stroke-width="1.2" opacity="0.95" class="code-line"/>
            <line x1="40" y1="55" x2="90" y2="55" stroke="#66BBDD" stroke-width="1" opacity="0.7"/>
            <line x1="44" y1="60" x2="74" y2="60" stroke="#4499DD" stroke-width="1.2" opacity="0.9" class="code-line"/>
            <line x1="40" y1="65" x2="84" y2="65" stroke="#88DDFF" stroke-width="1" opacity="0.6"/>
            <line x1="44" y1="70" x2="76" y2="70" stroke="#4499DD" stroke-width="1.2" opacity="0.95" class="code-line"/>
            <line x1="40" y1="75" x2="82" y2="75" stroke="#66BBDD" stroke-width="1" opacity="0.65"/>
            <!-- Keyboard bar -->
            <rect x="34" y="86" width="70" height="9" rx="2" fill="#1E1E2E" stroke="#18182A" stroke-width="0.8"/>
            <rect x="50" y="88" width="38" height="5" rx="1" fill="#141422" opacity="0.9"/>
            <!-- Papers (right side) -->
            <rect x="108" y="50" width="20" height="30" rx="1" fill="#EEE8D8" transform="rotate(7, 118, 65)"/>
            <rect x="111" y="53" width="20" height="30" rx="1" fill="#E4DCC8" transform="rotate(-4, 121, 68)"/>
          </g>

          <!-- ── REVIEWING: checklist + magnifying glass ── -->
          <g v-else-if="deskState === 'reviewing'" key="reviewing">
            <!-- Checklist paper -->
            <rect x="16" y="46" width="58" height="56" rx="2" fill="#F2EEE6" stroke="#D8D0C0" stroke-width="0.8" transform="rotate(1.5, 45, 74)"/>
            <!-- Checkbox 1 (checked) -->
            <rect x="22" y="56" width="8" height="8" rx="1" fill="none" stroke="#7788AA" stroke-width="1.2"/>
            <path d="M23.5,60 L25.5,62 L30,57" stroke="#4466BB" stroke-width="1.5" fill="none" stroke-linecap="round"/>
            <line x1="34" y1="60" x2="68" y2="60" stroke="#999090" stroke-width="0.8" opacity="0.7"/>
            <!-- Checkbox 2 (checked) -->
            <rect x="22" y="68" width="8" height="8" rx="1" fill="none" stroke="#7788AA" stroke-width="1.2"/>
            <path d="M23.5,72 L25.5,74 L30,69" stroke="#4466BB" stroke-width="1.5" fill="none" stroke-linecap="round"/>
            <line x1="34" y1="72" x2="66" y2="72" stroke="#999090" stroke-width="0.8" opacity="0.7"/>
            <!-- Checkbox 3 (unchecked — still reviewing) -->
            <rect x="22" y="80" width="8" height="8" rx="1" fill="none" stroke="#9988AA" stroke-width="1"/>
            <line x1="34" y1="84" x2="62" y2="84" stroke="#999090" stroke-width="0.8" opacity="0.5"/>

            <!-- Magnifying glass -->
            <circle cx="105" cy="72" r="17" fill="rgba(68,102,200,0.1)" stroke="#8896BB" stroke-width="2.2"/>
            <!-- Glass shine -->
            <path d="M97,65 Q100,63 105,65" stroke="white" stroke-width="1.5" fill="none" opacity="0.5" stroke-linecap="round"/>
            <!-- Handle -->
            <rect x="117.5" y="81.5" width="15" height="4.5" rx="2" fill="#8896BB" transform="rotate(42, 125, 84)"/>
          </g>

          <!-- ── FOLLOWUP: sticky note + flag ── -->
          <g v-else-if="deskState === 'followup'" key="followup">
            <!-- Background papers -->
            <rect x="14" y="50" width="50" height="52" rx="1" fill="#EAE4D4" stroke="#D4CCC0" stroke-width="0.8" transform="rotate(-3, 39, 76)"/>
            <line x1="20" y1="62" x2="58" y2="61" stroke="#A09880" stroke-width="0.7" opacity="0.6"/>
            <line x1="20" y1="69" x2="58" y2="68" stroke="#A09880" stroke-width="0.7" opacity="0.6"/>
            <line x1="20" y1="76" x2="54" y2="75" stroke="#A09880" stroke-width="0.7" opacity="0.5"/>

            <!-- Sticky note -->
            <rect x="64" y="48" width="48" height="48" rx="1" fill="#FFE44A" stroke="#D4C030" stroke-width="1" transform="rotate(2.5, 88, 72)"/>
            <!-- Fold corner -->
            <polygon points="112,48 112,61 99,48" fill="#D4C030" opacity="0.5" transform="rotate(2.5, 88, 72)"/>
            <!-- Lines on sticky -->
            <line x1="70" y1="63" x2="106" y2="63" stroke="#AA8800" stroke-width="1" opacity="0.55" transform="rotate(2.5, 88, 72)"/>
            <line x1="70" y1="71" x2="106" y2="71" stroke="#AA8800" stroke-width="1" opacity="0.5" transform="rotate(2.5, 88, 72)"/>
            <line x1="70" y1="79" x2="100" y2="79" stroke="#AA8800" stroke-width="1" opacity="0.45" transform="rotate(2.5, 88, 72)"/>

            <!-- Flag pin (top-right) -->
            <rect x="120" y="38" width="2.5" height="36" rx="1" fill="#BB2222"/>
            <polygon points="122.5,38 136,44 122.5,50" fill="#DD2222"/>
          </g>

          <!-- ── ERROR: warning symbol + scattered papers ── -->
          <g v-else-if="deskState === 'error'" key="error">
            <!-- Scattered papers -->
            <rect x="16" y="52" width="46" height="50" rx="1" fill="#EAE0D0" transform="rotate(13, 39, 77)" opacity="0.9"/>
            <rect x="28" y="58" width="46" height="50" rx="1" fill="#E4D8C8" transform="rotate(-9, 51, 83)" opacity="0.85"/>
            <!-- Warning triangle -->
            <polygon points="70,46 108,98 32,98" fill="#CC2222" opacity="0.12"/>
            <polygon points="70,46 108,98 32,98" fill="none" stroke="#CC2222" stroke-width="2.5" opacity="0.85"/>
            <!-- Exclamation mark -->
            <rect x="67.5" y="60" width="5" height="22" rx="2" fill="#CC2222" opacity="0.9"/>
            <circle cx="70" cy="89" r="3.5" fill="#CC2222" opacity="0.9"/>
          </g>
        </Transition>

        <!-- Desk front edge (3D face — rendered on top of items) -->
        <rect x="8" y="100" width="124" height="12" rx="1" fill="#4a2e10"/>
        <!-- Desk legs -->
        <rect x="12" y="110" width="8" height="24" rx="1" fill="#3a2008"/>
        <rect x="120" y="110" width="8" height="24" rx="1" fill="#3a2008"/>

        <!-- ── Worker figure (rendered last = on top) ── -->
        <!-- Torso / shirt (just above desk line) -->
        <rect x="55" y="30" width="30" height="10" rx="5" :fill="avatarBodyColor"/>
        <!-- Neck -->
        <rect x="65" y="26" width="10" height="7" rx="3" :fill="avatarSkinColor"/>
        <!-- Head -->
        <circle cx="70" cy="17" r="12" :fill="avatarSkinColor" class="avatar-head"/>
        <!-- Hair -->
        <ellipse cx="70" cy="9" rx="10" ry="7" :fill="avatarHairColor"/>
        <!-- Eyes -->
        <circle cx="65.5" cy="16" r="1.8" fill="#3A2010" opacity="0.85"/>
        <circle cx="74.5" cy="16" r="1.8" fill="#3A2010" opacity="0.85"/>
        <!-- Small smile (idle/working only) -->
        <path
          v-if="deskState !== 'error'"
          d="M67,21 Q70,24 73,21"
          stroke="#3A2010" stroke-width="0.9" fill="none" opacity="0.5" stroke-linecap="round"
        />
        <!-- Concerned frown (error) -->
        <path
          v-if="deskState === 'error'"
          d="M67,23 Q70,20 73,23"
          stroke="#3A2010" stroke-width="0.9" fill="none" opacity="0.6" stroke-linecap="round"
        />
      </template>
    </svg>

    <!-- Footer: name + state badge -->
    <div class="desk-footer">
      <span class="desk-name">{{ worker.name }}</span>
      <span class="desk-state-chip" :class="`chip-${deskState}`">{{ stateLabel }}</span>
    </div>

    <!-- Task info card (shown when a task is active) -->
    <div
      v-if="task && deskState !== 'offline'"
      class="desk-task"
      @click.stop="onTaskClick"
    >
      <span class="desk-task-name">{{ task.name || task.id.slice(-8) }}</span>
      <div class="desk-task-meta">
        <span class="desk-task-state" :class="`task-st-${task.state.replace(/[^a-z]/g, '-')}`">
          {{ task.state }}
        </span>
        <a
          v-if="task.pr_url"
          :href="task.pr_url"
          target="_blank"
          rel="noopener"
          class="desk-pr-link"
          @click.stop
        >PR↗</a>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, getCurrentInstance } from 'vue'
import type { Worker, Task } from '../../types'
import { useTasksStore } from '../../stores/tasks'

const props = withDefaults(
  defineProps<{
    worker: Worker
    selected?: boolean
  }>(),
  { selected: false },
)

const emit = defineEmits<{
  click: []
  taskClick: [taskId: string]
}>()

const tasksStore = useTasksStore()

const uid = getCurrentInstance()?.uid ?? Math.floor(Math.random() * 1e6)

const task = computed<Task | null>(
  () =>
    tasksStore.liveTasks.find(
      (t) => t.worker_id === props.worker.id && !['done', 'failed', 'cancelled'].includes(t.state),
    ) ?? null,
)

type DeskState = 'idle' | 'working' | 'reviewing' | 'followup' | 'error' | 'offline'

const deskState = computed((): DeskState => {
  const s = props.worker.state
  if (s === 'offline') return 'offline'
  if (s === 'error') return 'error'
  if (s === 'awaiting-review') return 'reviewing'
  if (task.value?.state === 'followup') return 'followup'
  if (s === 'idle') return 'idle'
  return 'working' // thinking | working | busy
})

const stateLabel = computed(() => {
  const labels: Record<DeskState, string> = {
    idle: 'idle',
    working: 'working',
    reviewing: 'review',
    followup: 'follow-up',
    error: 'error',
    offline: 'offline',
  }
  return labels[deskState.value]
})

const lampActive = computed(() =>
  deskState.value === 'working' || deskState.value === 'reviewing' || deskState.value === 'followup',
)

const lampShadeColor = computed(() => {
  if (deskState.value === 'reviewing') return '#334488'
  if (lampActive.value) return '#CC8800'
  return '#443300'
})

const lampGlowColor = computed(() => {
  if (deskState.value === 'reviewing') return '#4488FF'
  return '#FF9900'
})

// Derive avatar colors from worker ID
const palette = [
  { skin: '#E8C9A0', hair: '#3A2010', body: '#CC7700' },
  { skin: '#E8C9A0', hair: '#222222', body: '#0088BB' },
  { skin: '#E8C9A0', hair: '#5A3A1A', body: '#AA2244' },
  { skin: '#E8C9A0', hair: '#442200', body: '#228844' },
  { skin: '#E8C9A0', hair: '#1A3050', body: '#7700CC' },
  { skin: '#DDBF9A', hair: '#332210', body: '#CC4400' },
  { skin: '#C8A080', hair: '#1A1A1A', body: '#005588' },
  { skin: '#E0B090', hair: '#2A1810', body: '#886600' },
]

function hashId(str: string) {
  let h = 0
  for (let i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) & 0xffff
  return h
}

const avatarPalette = computed(() => palette[hashId(props.worker.id) % palette.length])
const avatarSkinColor = computed(() => avatarPalette.value.skin)
const avatarHairColor = computed(() => avatarPalette.value.hair)
const avatarBodyColor = computed(() => avatarPalette.value.body)

function onTaskClick() {
  if (task.value) emit('taskClick', task.value.id)
}
</script>

<style scoped>
.worker-desk {
  background: #0e0800;
  border: 1px solid rgba(200, 140, 0, 0.2);
  border-radius: 4px;
  display: flex;
  flex-direction: column;
  cursor: pointer;
  transition:
    border-color 0.4s ease,
    box-shadow 0.4s ease,
    background 0.4s ease;
  overflow: hidden;
  user-select: none;
}

.worker-desk:hover {
  border-color: rgba(200, 140, 0, 0.45);
  background: #130d00;
}

.worker-desk.selected {
  border-color: var(--color-teal, #00bbaa);
  box-shadow: 0 0 12px rgba(0, 187, 170, 0.25);
}

/* State-specific border glows */
.worker-desk.state-working {
  border-color: rgba(68, 180, 68, 0.4);
  box-shadow: 0 0 14px rgba(68, 180, 68, 0.12);
}
.worker-desk.state-reviewing {
  border-color: rgba(68, 136, 255, 0.4);
  box-shadow: 0 0 14px rgba(68, 136, 255, 0.12);
}
.worker-desk.state-followup {
  border-color: rgba(255, 200, 0, 0.4);
  box-shadow: 0 0 14px rgba(255, 200, 0, 0.12);
}
.worker-desk.state-error {
  border-color: rgba(220, 50, 30, 0.5);
  box-shadow: 0 0 16px rgba(220, 50, 30, 0.18);
}
.worker-desk.state-offline {
  border-color: rgba(80, 60, 40, 0.25);
  opacity: 0.65;
}

/* SVG fills the cell */
.desk-svg {
  width: 100%;
  height: auto;
  display: block;
}

/* Lamp glow pulse */
.lamp-glow {
  animation: lampPulse 2s ease-in-out infinite;
}
.state-working .lamp-glow {
  animation-duration: 1.4s;
}
.state-followup .lamp-glow {
  animation-duration: 1s;
}

@keyframes lampPulse {
  0%, 100% { opacity: 0.15; }
  50% { opacity: 0.28; }
}

/* Lamp shade CSS transition (fill is set via inline style) */
.lamp-shade {
  transition: fill 0.5s ease;
}

/* Desk item fade transition */
.desk-fade-enter-active,
.desk-fade-leave-active {
  transition: opacity 0.35s ease;
}
.desk-fade-enter-from,
.desk-fade-leave-to {
  opacity: 0;
}

/* Avatar head subtle bob when working */
.state-working .avatar-head {
  animation: headBob 2.2s ease-in-out infinite;
}
.state-followup .avatar-head {
  animation: headBob 1.8s ease-in-out infinite;
}
@keyframes headBob {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-1.5px); }
}

/* Footer */
.desk-footer {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 8px;
  border-top: 1px solid rgba(200, 140, 0, 0.12);
  flex-shrink: 0;
}

.desk-name {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-text-dim, #888);
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  letter-spacing: 0.5px;
}

.desk-state-chip {
  font-family: var(--font-pixel);
  font-size: 5px;
  letter-spacing: 0.5px;
  padding: 1px 5px;
  border-radius: 2px;
  white-space: nowrap;
  flex-shrink: 0;
  text-transform: uppercase;
  transition: background 0.4s ease, color 0.4s ease;
}
.chip-idle        { background: rgba(100,100,100,0.25); color: #888; }
.chip-working     { background: rgba(68,180,68,0.2);   color: #88dd44; }
.chip-reviewing   { background: rgba(68,136,255,0.2);  color: #88aaff; }
.chip-followup    { background: rgba(255,200,0,0.2);   color: #ffcc44; }
.chip-error       { background: rgba(220,50,30,0.2);   color: #ff7766; }
.chip-offline     { background: rgba(60,40,20,0.3);    color: #664422; }

/* Task card */
.desk-task {
  padding: 5px 8px 6px;
  border-top: 1px solid rgba(200, 140, 0, 0.12);
  background: rgba(0, 0, 0, 0.2);
  cursor: pointer;
  transition: background 0.15s;
  flex-shrink: 0;
}
.desk-task:hover {
  background: rgba(200, 140, 0, 0.06);
}

.desk-task-name {
  font-family: var(--font-pixel);
  font-size: 5.5px;
  color: var(--color-text, #ccc);
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  letter-spacing: 0.3px;
  margin-bottom: 3px;
}

.desk-task-meta {
  display: flex;
  align-items: center;
  gap: 5px;
}

.desk-task-state {
  font-family: var(--font-pixel);
  font-size: 5px;
  letter-spacing: 0.3px;
  text-transform: uppercase;
  opacity: 0.7;
  padding: 1px 3px;
  border-radius: 1px;
}
.task-st-working         { color: var(--color-green, #88dd22); }
.task-st-awaiting-review { color: var(--color-amber, #ffcc00); }
.task-st-followup        { color: var(--color-orange, #ff7700); }
.task-st-pending         { color: var(--color-text-dim, #888); }
.task-st-failed          { color: var(--color-red, #ee3322); }
.task-st-done            { color: var(--color-teal, #00bbaa); }

.desk-pr-link {
  font-family: var(--font-pixel);
  font-size: 5px;
  color: var(--color-teal, #00bbaa);
  text-decoration: none;
  padding: 1px 3px;
  border: 1px solid rgba(0, 187, 170, 0.3);
  border-radius: 1px;
  letter-spacing: 0.3px;
  transition: color 0.15s, border-color 0.15s;
  margin-left: auto;
}
.desk-pr-link:hover {
  color: #00ddcc;
  border-color: rgba(0, 221, 204, 0.5);
}
</style>
