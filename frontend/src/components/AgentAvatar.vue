<template>
  <div class="avatar-wrapper" :class="[agent.state, agent.type]" :title="`${agent.name} [${agent.state}]`">
    <div class="avatar-body">
      <div class="avatar-head">
        <div class="avatar-eye left"></div>
        <div class="avatar-eye right"></div>
        <div v-if="agent.type === 'overseer'" class="avatar-crown">♛</div>
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
    <div v-if="agent.state === 'thinking'" class="think-bubble">...</div>
  </div>
</template>

<script setup>
defineProps({
  agent: {
    type: Object,
    required: true
  }
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
  width: 24px;
  height: 20px;
  background: #5a4020;
  border: 2px solid var(--color-brass-dark);
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-around;
}

.avatar-eye {
  width: 4px;
  height: 4px;
  background: var(--color-amber);
  border-radius: 50%;
}

.avatar-crown {
  position: absolute;
  top: -14px;
  left: 50%;
  transform: translateX(-50%);
  font-size: 16px;
  color: var(--color-brass-light);
  filter: drop-shadow(0 0 4px var(--color-brass));
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
  background: #5a4020;
  border: 1px solid var(--color-brass-dark);
}

.avatar-chest {
  flex: 1;
  height: 18px;
  background: #4a3015;
  border: 2px solid var(--color-brass-dark);
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
.avatar-led.thinking { background: var(--color-blue); box-shadow: 0 0 6px var(--color-blue); }
.avatar-led.working { background: var(--color-green); box-shadow: 0 0 6px var(--color-green); }
.avatar-led.busy { background: var(--color-orange); box-shadow: 0 0 6px var(--color-orange); }
.avatar-led.error { background: var(--color-red); box-shadow: 0 0 6px var(--color-red); }

/* Legs */
.avatar-legs {
  display: flex;
  gap: 4px;
}

.avatar-leg {
  width: 8px;
  height: 12px;
  background: #5a4020;
  border: 1px solid var(--color-brass-dark);
}

/* Label */
.avatar-label {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-text-dim);
  text-align: center;
  max-width: 60px;
  overflow: hidden;
  white-space: nowrap;
}

/* Think bubble */
.think-bubble {
  position: absolute;
  top: -20px;
  right: -10px;
  background: rgba(68, 153, 255, 0.2);
  border: 1px solid var(--color-blue);
  border-radius: 4px;
  font-size: 10px;
  color: var(--color-blue);
  padding: 2px 5px;
  animation: bubblePop 1s infinite;
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
  background: #3a2810;
  border-color: var(--color-brass);
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
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.6; transform: scale(0.9); }
}
</style>
