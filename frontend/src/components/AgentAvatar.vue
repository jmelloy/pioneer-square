<template>
  <div
    class="avatar-wrapper"
    :class="[agent.state, agent.type]"
    :style="`--agent-color: ${agentColor}; --agent-color-dim: ${agentColorDim}`"
    :title="`${agent.name} [${agent.state}]`"
  >
    <svg
      class="avatar-svg"
      viewBox="0 0 40 56"
      xmlns="http://www.w3.org/2000/svg"
      overflow="visible"
    >
      <!-- HEAD GROUP — tilt/shake animations target this whole group -->
      <g class="head-group">

        <!-- Crown (foreman only) — three prongs with gem accents -->
        <g v-if="agent.type === 'foreman'" class="crown">
          <rect x="9" y="3" width="22" height="2" rx="1" fill="var(--color-gold, #ffd644)" />
          <rect x="12" y="-1" width="4" height="5" rx="1" fill="var(--color-gold, #ffd644)" />
          <rect x="18" y="-4" width="4" height="8" rx="1" fill="var(--color-gold, #ffd644)" />
          <rect x="24" y="-1" width="4" height="5" rx="1" fill="var(--color-gold, #ffd644)" />
          <circle cx="14" cy="-0.5" r="1.2" fill="var(--color-red, #ee3322)" />
          <circle cx="20" cy="-3"   r="1.2" fill="var(--color-blue, #44aaee)" />
          <circle cx="26" cy="-0.5" r="1.2" fill="var(--color-green, #88dd22)" />
        </g>

        <!-- Antenna (workers) -->
        <template v-if="agent.type !== 'foreman'">
          <rect x="19" y="1" width="2" height="3" fill="var(--agent-color)" />
          <circle class="antenna-ball" cx="20" cy="1" r="2.2" fill="var(--agent-color)" />
        </template>

        <!-- Head box -->
        <rect class="head-box" x="9" y="5" width="22" height="18" rx="2"
              fill="rgba(18,9,0,0.93)" stroke="var(--agent-color)" stroke-width="1.5" />

        <!-- Eyes -->
        <rect class="eye" x="12" y="10" width="6" height="5" rx="1" />
        <rect class="eye" x="22" y="10" width="6" height="5" rx="1" />

        <!-- Ear bolts -->
        <circle cx="10" cy="14" r="1.2" fill="var(--agent-color)" opacity="0.75" />
        <circle cx="30" cy="14" r="1.2" fill="var(--agent-color)" opacity="0.75" />

        <!-- Speaker grill -->
        <rect x="13" y="20" width="3" height="1" rx="0.5" fill="var(--agent-color)" opacity="0.65" />
        <rect x="18" y="20" width="3" height="1" rx="0.5" fill="var(--agent-color)" opacity="0.65" />
        <rect x="23" y="20" width="3" height="1" rx="0.5" fill="var(--agent-color)" opacity="0.65" />
      </g>

      <!-- Neck -->
      <rect x="16" y="23" width="8" height="3" rx="1" fill="var(--agent-color)" opacity="0.45" />

      <!-- Body -->
      <rect x="7" y="26" width="26" height="18" rx="2"
            fill="rgba(18,9,0,0.93)" stroke="var(--agent-color)" stroke-width="1.5" />
      <!-- Chest panel recess -->
      <rect x="13" y="29" width="14" height="12" rx="1"
            fill="rgba(0,0,0,0.65)" stroke="var(--agent-color)" stroke-width="0.75" />
      <!-- LED indicator -->
      <circle class="led" :class="agent.state" cx="20" cy="35" r="3.5" />
      <!-- Shoulder rivets -->
      <circle cx="9"  cy="28" r="1.5" fill="var(--agent-color)" opacity="0.85" />
      <circle cx="31" cy="28" r="1.5" fill="var(--agent-color)" opacity="0.85" />
      <!-- Side exhaust vents -->
      <rect x="29" y="33" width="3.5" height="1" rx="0.5" fill="var(--agent-color)" opacity="0.55" />
      <rect x="29" y="36" width="3.5" height="1" rx="0.5" fill="var(--agent-color)" opacity="0.55" />
      <rect x="29" y="39" width="3.5" height="1" rx="0.5" fill="var(--agent-color)" opacity="0.55" />
      <!-- Belt line -->
      <rect x="7" y="42" width="26" height="2" rx="0"
            fill="rgba(0,0,0,0.35)" stroke="var(--agent-color)" stroke-width="0.5" />

      <!-- Left arm -->
      <g class="left-arm">
        <rect x="1"  y="27" width="6" height="12" rx="2"
              fill="rgba(18,9,0,0.93)" stroke="var(--agent-color)" stroke-width="1.5" />
        <circle cx="4" cy="31" r="1.5" fill="var(--agent-color)" opacity="0.7" />
        <rect x="0"  y="38" width="8"  height="5"  rx="2"
              fill="rgba(18,9,0,0.93)" stroke="var(--agent-color)" stroke-width="1.5" />
      </g>

      <!-- Right arm -->
      <g class="right-arm">
        <rect x="33" y="27" width="6" height="12" rx="2"
              fill="rgba(18,9,0,0.93)" stroke="var(--agent-color)" stroke-width="1.5" />
        <circle cx="36" cy="31" r="1.5" fill="var(--agent-color)" opacity="0.7" />
        <rect x="32"  y="38" width="8"  height="5"  rx="2"
              fill="rgba(18,9,0,0.93)" stroke="var(--agent-color)" stroke-width="1.5" />
      </g>

      <!-- Legs -->
      <rect class="left-leg"  x="10" y="44" width="9" height="10" rx="2"
            fill="rgba(18,9,0,0.93)" stroke="var(--agent-color)" stroke-width="1.5" />
      <rect class="right-leg" x="21" y="44" width="9" height="10" rx="2"
            fill="rgba(18,9,0,0.93)" stroke="var(--agent-color)" stroke-width="1.5" />
      <!-- Feet -->
      <rect x="8"  y="51" width="13" height="5" rx="2"
            fill="rgba(18,9,0,0.93)" stroke="var(--agent-color)" stroke-width="1.5" />
      <rect x="19" y="51" width="13" height="5" rx="2"
            fill="rgba(18,9,0,0.93)" stroke="var(--agent-color)" stroke-width="1.5" />
    </svg>

    <div class="avatar-label">{{ agent.name.slice(0, 8) }}</div>
    <div v-if="agent.state === 'thinking'" class="think-bubble">💭</div>
    <div v-if="agent.state === 'working'"  class="work-burst">⭐</div>
    <div v-if="agent.state === 'error'"    class="error-burst">!</div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  agent: {
    type: Object,
    required: true
  }
})

/* Warm SNES/CT palette — gold, teal, orange, sky, red, lime, copper, amber */
const palette = [
  { color: '#ffd644', dim: 'rgba(255,214,68,0.2)' },
  { color: '#00bbaa', dim: 'rgba(0,187,170,0.2)' },
  { color: '#ff7700', dim: 'rgba(255,119,0,0.2)' },
  { color: '#44aaee', dim: 'rgba(68,170,238,0.2)' },
  { color: '#ee3322', dim: 'rgba(238,51,34,0.2)' },
  { color: '#88dd22', dim: 'rgba(136,221,34,0.2)' },
  { color: '#ee7722', dim: 'rgba(238,119,34,0.2)' },
  { color: '#ffcc00', dim: 'rgba(255,204,0,0.2)' },
]

function hashAgent(str) {
  let hash = 0
  for (let i = 0; i < str.length; i++) {
    hash = (hash * 31 + str.charCodeAt(i)) & 0xffff
  }
  return hash
}

const agentColor = computed(() => {
  const idx = hashAgent(props.agent.id || props.agent.name || '') % palette.length
  return palette[idx].color
})

const agentColorDim = computed(() => {
  const idx = hashAgent(props.agent.id || props.agent.name || '') % palette.length
  return palette[idx].dim
})
</script>

<style scoped>
.avatar-wrapper {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  cursor: default;
  user-select: none;
  position: relative;
}

.avatar-svg {
  width: 44px;
  height: auto;
  image-rendering: pixelated;
  overflow: visible;
}

/* ── Eyes ─────────────────────────────────────────────── */
.eye { fill: var(--color-cream, #ffe8c0); }

.avatar-wrapper.thinking .eye {
  fill: var(--color-blue, #44aaee);
  filter: drop-shadow(0 0 2px var(--color-blue, #44aaee));
}
.avatar-wrapper.working .eye {
  fill: var(--color-green, #88dd22);
}
.avatar-wrapper.error .eye {
  fill: var(--color-red, #ee3322);
  filter: drop-shadow(0 0 2px var(--color-red, #ee3322));
}

/* slow idle blink */
.avatar-wrapper.idle .eye {
  animation: eyeBlink 6s infinite;
}
@keyframes eyeBlink {
  0%, 82%, 100% { opacity: 1; }
  86%           { opacity: 0.05; }
  90%           { opacity: 1; }
  93%           { opacity: 0.05; }
  97%           { opacity: 1; }
}

/* ── LED ──────────────────────────────────────────────── */
.led.idle     { fill: var(--color-text-dim, #554433); }
.led.thinking { fill: var(--color-blue,   #44aaee); filter: drop-shadow(0 0 4px var(--color-blue,   #44aaee)); }
.led.working  { fill: var(--color-green,  #88dd22); filter: drop-shadow(0 0 4px var(--color-green,  #88dd22));
                transform-box: fill-box; transform-origin: center; animation: ledPulse 0.5s infinite alternate; }
.led.busy     { fill: var(--color-orange, #ff7700); filter: drop-shadow(0 0 4px var(--color-orange, #ff7700)); }
.led.error    { fill: var(--color-red,    #ee3322); filter: drop-shadow(0 0 4px var(--color-red,    #ee3322)); }

@keyframes ledPulse {
  from { transform: scale(1); }
  to   { transform: scale(1.55); }
}

/* ── Antenna pulse when thinking ─────────────────────── */
.avatar-wrapper.thinking .antenna-ball {
  animation: antennaPulse 1.5s infinite alternate;
}
@keyframes antennaPulse {
  from { opacity: 0.5; }
  to   { opacity: 1;   filter: drop-shadow(0 0 3px var(--agent-color)); }
}

/* ── Foreman head highlight ───────────────────────────── */
.avatar-wrapper.foreman .head-box {
  stroke: var(--color-gold, #ffd644);
  stroke-width: 2;
  filter: drop-shadow(0 0 4px var(--color-gold, #ffd644));
}

/* ── Whole-wrapper state glows ────────────────────────── */
.avatar-wrapper.thinking { filter: drop-shadow(0 0 6px var(--color-blue,   #44aaee)); }
.avatar-wrapper.working  { filter: drop-shadow(0 0 6px var(--color-green,  #88dd22)); }
.avatar-wrapper.busy     { filter: drop-shadow(0 0 6px var(--color-orange, #ff7700)); }
.avatar-wrapper.error    { filter: drop-shadow(0 0 8px var(--color-red,    #ee3322)); }

/* ── State body animations ────────────────────────────── */

/* thinking: head tilts side to side */
.avatar-wrapper.thinking .head-group {
  transform-box: fill-box;
  transform-origin: bottom center;
  animation: tiltHead 1.5s infinite ease-in-out;
}
@keyframes tiltHead {
  0%, 100% { transform: rotate(-5deg); }
  50%       { transform: rotate(5deg); }
}

/* working: whole robot bounces */
.avatar-wrapper.working .avatar-svg {
  animation: workBounce 0.4s infinite alternate;
}
@keyframes workBounce {
  from { transform: translateY(0); }
  to   { transform: translateY(-4px); }
}

/* busy: arms swing out of phase */
.avatar-wrapper.busy .left-arm {
  transform-box: fill-box;
  transform-origin: top right;
  animation: leftArmSwing 0.5s infinite alternate;
}
.avatar-wrapper.busy .right-arm {
  transform-box: fill-box;
  transform-origin: top left;
  animation: rightArmSwing 0.5s infinite alternate;
}
@keyframes leftArmSwing {
  from { transform: rotate(-22deg); }
  to   { transform: rotate(22deg); }
}
@keyframes rightArmSwing {
  from { transform: rotate(22deg); }
  to   { transform: rotate(-22deg); }
}

/* error: head shakes */
.avatar-wrapper.error .head-group {
  animation: shake 0.22s infinite;
}
@keyframes shake {
  0%, 100% { transform: translateX(0); }
  25%       { transform: translateX(-3px); }
  75%       { transform: translateX(3px); }
}

/* ── Label ────────────────────────────────────────────── */
.avatar-label {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--agent-color, var(--color-text-dim));
  text-align: center;
  max-width: 60px;
  overflow: hidden;
  white-space: nowrap;
  text-shadow: 0 0 4px var(--agent-color, transparent);
}

/* ── Overlaid emoji/text bursts ───────────────────────── */
.think-bubble {
  position: absolute;
  top: -20px;
  right: -14px;
  font-size: 20px;
  animation: bubblePop 1.2s infinite ease-in-out;
  filter: drop-shadow(0 0 4px var(--color-blue, #44aaee));
}

.work-burst {
  position: absolute;
  top: -18px;
  right: -10px;
  font-size: 14px;
  animation: burstSpin 1s infinite linear;
  filter: drop-shadow(0 0 4px var(--color-amber, #ffcc00));
}

.error-burst {
  position: absolute;
  top: -18px;
  right: -6px;
  font-family: var(--font-pixel);
  font-size: 12px;
  font-weight: bold;
  color: var(--color-red, #ee3322);
  animation: shake 0.2s infinite;
  text-shadow: 0 0 6px var(--color-red, #ee3322);
}

@keyframes bubblePop {
  0%, 100% { opacity: 1;   transform: scale(1)    rotate(-5deg); }
  50%       { opacity: 0.7; transform: scale(0.85) rotate(5deg); }
}

@keyframes burstSpin {
  from { transform: rotate(0deg)   scale(1); }
  50%  { transform: rotate(180deg) scale(1.2); }
  to   { transform: rotate(360deg) scale(1); }
}
</style>
