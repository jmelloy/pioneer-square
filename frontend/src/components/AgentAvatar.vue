<template>
  <div
    class="avatar-wrapper"
    :class="[agent.state, agent.type]"
    :style="`--agent-color: ${agentColor}; --agent-color-dim: ${agentColorDim}`"
    :title="`${agent.name} [${agent.state}]`"
  >
    <div class="avatar-body">
      <div class="avatar-head">
        <div class="avatar-eye left"></div>
        <div class="avatar-eye right"></div>
        <div v-if="agent.type === 'overseer'" class="avatar-crown">👑</div>
      </div>
      <div class="avatar-torso">
        <div class="avatar-arm left"></div>
        <div class="avatar-chest">
          <div class="avatar-led" :class="agent.state"></div>
        </div>
        <div class="avatar-arm right"></div>
      </div>
      <div class="avatar-legs">
        <div class="avatar-leg left"></div>
        <div class="avatar-leg right"></div>
      </div>
    </div>
    <div class="avatar-label">{{ agent.name.slice(0, 8) }}</div>
    <div v-if="agent.state === 'thinking'" class="think-bubble">💭</div>
    <div v-if="agent.state === 'working'" class="work-burst">⭐</div>
    <div v-if="agent.state === 'error'" class="error-burst">!</div>
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

const palette = [
  { color: '#ff6bb3', dim: 'rgba(255,107,179,0.2)' },
  { color: '#66ccff', dim: 'rgba(102,204,255,0.2)' },
  { color: '#ffe566', dim: 'rgba(255,229,102,0.2)' },
  { color: '#00ffb3', dim: 'rgba(0,255,179,0.2)' },
  { color: '#c07aff', dim: 'rgba(192,122,255,0.2)' },
  { color: '#ff9a6c', dim: 'rgba(255,154,108,0.2)' },
  { color: '#d9a6ff', dim: 'rgba(217,166,255,0.2)' },
  { color: '#ff6b8a', dim: 'rgba(255,107,138,0.2)' },
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

.avatar-body {
  image-rendering: pixelated;
  display: flex;
  flex-direction: column;
  align-items: center;
}

/* Head */
.avatar-head {
  width: 22px;
  height: 20px;
  background: var(--agent-color-dim, rgba(192, 122, 255, 0.15));
  border: 2px solid var(--agent-color, #c07aff);
  border-radius: 5px;
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-around;
  box-shadow: 0 0 6px var(--agent-color, transparent);
}

.avatar-eye {
  width: 5px;
  height: 5px;
  background: white;
  border-radius: 50%;
  box-shadow: 0 0 3px white, 0 0 6px rgba(255, 255, 255, 0.5);
}

.avatar-crown {
  position: absolute;
  top: -16px;
  left: 50%;
  transform: translateX(-50%);
  font-size: 14px;
  filter: drop-shadow(0 0 4px #ffe566);
}

/* Torso */
.avatar-torso {
  width: 28px;
  height: 18px;
  display: flex;
  align-items: center;
  gap: 0;
}

.avatar-arm {
  width: 5px;
  height: 14px;
  background: var(--agent-color-dim, rgba(192, 122, 255, 0.15));
  border: 1px solid var(--agent-color, #c07aff);
  border-radius: 2px;
}

.avatar-chest {
  flex: 1;
  height: 18px;
  background: rgba(10, 9, 24, 0.85);
  border: 2px solid var(--agent-color, #c07aff);
  display: flex;
  align-items: center;
  justify-content: center;
}

.avatar-led {
  width: 6px;
  height: 6px;
  border-radius: 50%;
}

.avatar-led.idle { background: var(--color-text-dim); }
.avatar-led.thinking { background: var(--color-blue); box-shadow: 0 0 8px var(--color-blue); }
.avatar-led.working { background: var(--color-green); box-shadow: 0 0 8px var(--color-green); animation: ledPulse 0.5s infinite alternate; }
.avatar-led.busy { background: var(--color-orange); box-shadow: 0 0 8px var(--color-orange); }
.avatar-led.error { background: var(--color-red); box-shadow: 0 0 8px var(--color-red); }

@keyframes ledPulse {
  from { transform: scale(1); }
  to { transform: scale(1.4); box-shadow: 0 0 14px var(--color-green); }
}

/* Legs */
.avatar-legs {
  display: flex;
  gap: 4px;
}

.avatar-leg {
  width: 8px;
  height: 12px;
  background: var(--agent-color-dim, rgba(192, 122, 255, 0.15));
  border: 1px solid var(--agent-color, #c07aff);
  border-radius: 2px 2px 3px 3px;
}

/* Label */
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

/* Think bubble */
.think-bubble {
  position: absolute;
  top: -20px;
  right: -14px;
  font-size: 20px;
  animation: bubblePop 1.2s infinite ease-in-out;
  filter: drop-shadow(0 0 4px var(--color-blue));
}

/* Work burst */
.work-burst {
  position: absolute;
  top: -18px;
  right: -10px;
  font-size: 14px;
  animation: burstSpin 1s infinite linear;
  filter: drop-shadow(0 0 4px var(--color-yellow));
}

/* Error burst */
.error-burst {
  position: absolute;
  top: -18px;
  right: -6px;
  font-family: var(--font-pixel);
  font-size: 12px;
  font-weight: bold;
  color: var(--color-red);
  animation: shake 0.2s infinite;
  text-shadow: 0 0 6px var(--color-red);
}

/* State animations */
.avatar-wrapper.thinking .avatar-head {
  animation: tiltHead 1.5s infinite ease-in-out;
}

.avatar-wrapper.working .avatar-body {
  animation: workBounce 0.4s infinite alternate;
}

.avatar-wrapper.busy .avatar-arm.left {
  animation: leftArmSwing 0.5s infinite alternate;
  transform-origin: top center;
}

.avatar-wrapper.busy .avatar-arm.right {
  animation: rightArmSwing 0.5s infinite alternate;
  transform-origin: top center;
}

.avatar-wrapper.error .avatar-head {
  animation: shake 0.3s infinite;
}

.avatar-wrapper.thinking {
  filter: drop-shadow(0 0 6px var(--color-blue));
}

.avatar-wrapper.working {
  filter: drop-shadow(0 0 6px var(--color-green));
}

.avatar-wrapper.busy {
  filter: drop-shadow(0 0 6px var(--color-orange));
}

.avatar-wrapper.error {
  filter: drop-shadow(0 0 8px var(--color-red));
}

.avatar-wrapper.overseer .avatar-head {
  border-color: #ffe566;
  box-shadow: 0 0 8px #ffe566;
}

@keyframes tiltHead {
  0%, 100% { transform: rotate(-5deg); }
  50% { transform: rotate(5deg); }
}

@keyframes workBounce {
  from { transform: translateY(0px); }
  to { transform: translateY(-3px); }
}

@keyframes leftArmSwing {
  from { transform: rotate(-20deg); }
  to { transform: rotate(20deg); }
}

@keyframes rightArmSwing {
  from { transform: rotate(20deg); }
  to { transform: rotate(-20deg); }
}

@keyframes shake {
  0%, 100% { transform: translateX(0); }
  25% { transform: translateX(-3px); }
  75% { transform: translateX(3px); }
}

@keyframes bubblePop {
  0%, 100% { opacity: 1; transform: scale(1) rotate(-5deg); }
  50% { opacity: 0.7; transform: scale(0.85) rotate(5deg); }
}

@keyframes burstSpin {
  from { transform: rotate(0deg) scale(1); }
  50% { transform: rotate(180deg) scale(1.2); }
  to { transform: rotate(360deg) scale(1); }
}
</style>
