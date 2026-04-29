<template>
  <div
    class="avatar-wrapper"
    :class="[agent.state, agent.type]"
    :style="`--agent-color: ${agentColor}; --agent-color-dim: ${agentColorDim}`"
    :title="`${agent.name} [${agent.state}]`"
  >
    <RobotWorker :state="agent.state" :type="agent.type" :walking="walking" />

    <div class="avatar-label">{{ agent.name.slice(0, 8) }}</div>
    <div v-if="agent.state === 'thinking'" class="think-bubble">💭</div>
    <div v-if="agent.state === 'working'"  class="work-burst">⭐</div>
    <div v-if="agent.state === 'error'"    class="error-burst">!</div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import RobotWorker from './sprites/RobotWorker.vue'

const props = defineProps({
  agent:   { type: Object,  required: true },
  walking: { type: Boolean, default: false },
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

/* State glows applied to the whole wrapper (sprite + label + bubbles) */
.avatar-wrapper.thinking { filter: drop-shadow(0 0 6px var(--color-blue,   #44aaee)); }
.avatar-wrapper.working  { filter: drop-shadow(0 0 6px var(--color-green,  #88dd22)); }
.avatar-wrapper.busy     { filter: drop-shadow(0 0 6px var(--color-orange, #ff7700)); }
.avatar-wrapper.error    { filter: drop-shadow(0 0 8px var(--color-red,    #ee3322)); }

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

@keyframes shake {
  0%, 100% { transform: translateX(0); }
  25%       { transform: translateX(-3px); }
  75%       { transform: translateX(3px); }
}
</style>
