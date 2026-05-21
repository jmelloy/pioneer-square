<template>
  <svg
    :class="['robot-sprite', state, type, { walking }]"
    viewBox="0 0 40 56"
    xmlns="http://www.w3.org/2000/svg"
    overflow="visible"
    shape-rendering="crispEdges"
  >
    <!-- HEAD GROUP — tilt/shake animations target this whole group -->
    <g class="head-group">
      <!-- Crown (foreman only) — three prongs with gem accents -->
      <g v-if="type === 'foreman'" class="crown">
        <rect x="9" y="3" width="22" height="2" rx="1" fill="var(--color-gold, #ffd644)" />
        <rect x="12" y="-1" width="4" height="5" rx="1" fill="var(--color-gold, #ffd644)" />
        <rect x="18" y="-4" width="4" height="8" rx="1" fill="var(--color-gold, #ffd644)" />
        <rect x="24" y="-1" width="4" height="5" rx="1" fill="var(--color-gold, #ffd644)" />
        <circle cx="14" cy="-0.5" r="1.2" fill="var(--color-red, #ee3322)" />
        <circle cx="20" cy="-3" r="1.2" fill="var(--color-blue, #44aaee)" />
        <circle cx="26" cy="-0.5" r="1.2" fill="var(--color-green, #88dd22)" />
      </g>

      <!-- Antenna (workers) -->
      <template v-if="type !== 'foreman'">
        <rect x="19" y="1" width="2" height="3" fill="var(--agent-color)" />
        <circle class="antenna-ball" cx="20" cy="1" r="2.2" fill="var(--agent-color)" />
      </template>

      <!-- Head box -->
      <rect
        class="head-box"
        x="9"
        y="5"
        width="22"
        height="18"
        rx="2"
        fill="rgba(18,9,0,0.93)"
        stroke="var(--agent-color)"
        stroke-width="1.5"
      />

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
    <rect
      x="7"
      y="26"
      width="26"
      height="18"
      rx="2"
      fill="rgba(18,9,0,0.93)"
      stroke="var(--agent-color)"
      stroke-width="1.5"
    />
    <!-- Chest panel recess -->
    <rect
      x="13"
      y="29"
      width="14"
      height="12"
      rx="1"
      fill="rgba(0,0,0,0.65)"
      stroke="var(--agent-color)"
      stroke-width="0.75"
    />
    <!-- LED indicator -->
    <circle class="led" cx="20" cy="35" r="3.5" />
    <!-- Shoulder rivets -->
    <circle cx="9" cy="28" r="1.5" fill="var(--agent-color)" opacity="0.85" />
    <circle cx="31" cy="28" r="1.5" fill="var(--agent-color)" opacity="0.85" />
    <!-- Side exhaust vents -->
    <rect x="29" y="33" width="3.5" height="1" rx="0.5" fill="var(--agent-color)" opacity="0.55" />
    <rect x="29" y="36" width="3.5" height="1" rx="0.5" fill="var(--agent-color)" opacity="0.55" />
    <rect x="29" y="39" width="3.5" height="1" rx="0.5" fill="var(--agent-color)" opacity="0.55" />
    <!-- Belt line -->
    <rect
      x="7"
      y="42"
      width="26"
      height="2"
      rx="0"
      fill="rgba(0,0,0,0.35)"
      stroke="var(--agent-color)"
      stroke-width="1"
    />

    <!-- Left arm -->
    <g class="left-arm">
      <rect
        x="1"
        y="27"
        width="6"
        height="12"
        rx="2"
        fill="rgba(18,9,0,0.93)"
        stroke="var(--agent-color)"
        stroke-width="1.5"
      />
      <circle cx="4" cy="31" r="1.5" fill="var(--agent-color)" opacity="0.7" />
      <rect
        x="0"
        y="38"
        width="8"
        height="5"
        rx="2"
        fill="rgba(18,9,0,0.93)"
        stroke="var(--agent-color)"
        stroke-width="1.5"
      />
    </g>

    <!-- Right arm -->
    <g class="right-arm">
      <rect
        x="33"
        y="27"
        width="6"
        height="12"
        rx="2"
        fill="rgba(18,9,0,0.93)"
        stroke="var(--agent-color)"
        stroke-width="1.5"
      />
      <circle cx="36" cy="31" r="1.5" fill="var(--agent-color)" opacity="0.7" />
      <rect
        x="32"
        y="38"
        width="8"
        height="5"
        rx="2"
        fill="rgba(18,9,0,0.93)"
        stroke="var(--agent-color)"
        stroke-width="1.5"
      />
    </g>

    <!-- Left leg + foot -->
    <g class="left-leg">
      <rect
        x="10"
        y="44"
        width="9"
        height="10"
        rx="2"
        fill="rgba(18,9,0,0.93)"
        stroke="var(--agent-color)"
        stroke-width="1.5"
      />
      <rect
        x="8"
        y="51"
        width="13"
        height="5"
        rx="2"
        fill="rgba(18,9,0,0.93)"
        stroke="var(--agent-color)"
        stroke-width="1.5"
      />
    </g>
    <!-- Right leg + foot -->
    <g class="right-leg">
      <rect
        x="21"
        y="44"
        width="9"
        height="10"
        rx="2"
        fill="rgba(18,9,0,0.93)"
        stroke="var(--agent-color)"
        stroke-width="1.5"
      />
      <rect
        x="19"
        y="51"
        width="13"
        height="5"
        rx="2"
        fill="rgba(18,9,0,0.93)"
        stroke="var(--agent-color)"
        stroke-width="1.5"
      />
    </g>
  </svg>
</template>

<script setup lang="ts">
withDefaults(
  defineProps<{
    state: string
    type: string
    walking?: boolean
  }>(),
  {
    walking: false,
  },
)
</script>

<style scoped>
.robot-sprite {
  width: 44px;
  height: auto;
  image-rendering: pixelated;
  overflow: visible;
}

/* ── Eyes ─────────────────────────────────────────────── */
.eye {
  fill: var(--color-cream, #ffe8c0);
}

.robot-sprite.thinking .eye {
  fill: var(--color-blue, #44aaee);
  filter: drop-shadow(0 0 2px var(--color-blue, #44aaee));
}
.robot-sprite.working .eye {
  fill: var(--color-green, #88dd22);
}
.robot-sprite.error .eye {
  fill: var(--color-red, #ee3322);
  filter: drop-shadow(0 0 2px var(--color-red, #ee3322));
}

.robot-sprite.idle .eye {
  animation: eyeBlink 6s infinite;
}
@keyframes eyeBlink {
  0%,
  82%,
  100% {
    opacity: 1;
  }
  86% {
    opacity: 0.05;
  }
  90% {
    opacity: 1;
  }
  93% {
    opacity: 0.05;
  }
  97% {
    opacity: 1;
  }
}

/* ── LED ──────────────────────────────────────────────── */
.led {
  fill: var(--color-text-dim, #554433);
}

.robot-sprite.thinking .led {
  fill: var(--color-blue, #44aaee);
  filter: drop-shadow(0 0 4px var(--color-blue, #44aaee));
}
.robot-sprite.working .led {
  fill: var(--color-green, #88dd22);
  filter: drop-shadow(0 0 4px var(--color-green, #88dd22));
  transform-box: fill-box;
  transform-origin: center;
  animation: ledPulse 0.5s infinite alternate;
}
.robot-sprite.busy .led {
  fill: var(--color-orange, #ff7700);
  filter: drop-shadow(0 0 4px var(--color-orange, #ff7700));
}
.robot-sprite.error .led {
  fill: var(--color-red, #ee3322);
  filter: drop-shadow(0 0 4px var(--color-red, #ee3322));
}

@keyframes ledPulse {
  from {
    transform: scale(1);
  }
  to {
    transform: scale(1.55);
  }
}

/* ── Antenna pulse when thinking ─────────────────────── */
.robot-sprite.thinking .antenna-ball {
  animation: antennaPulse 1.5s infinite alternate;
}
@keyframes antennaPulse {
  from {
    opacity: 0.5;
  }
  to {
    opacity: 1;
    filter: drop-shadow(0 0 3px var(--agent-color));
  }
}

/* ── Foreman head highlight ───────────────────────────── */
.robot-sprite.foreman .head-box {
  stroke: var(--color-gold, #ffd644);
  stroke-width: 2;
  filter: drop-shadow(0 0 4px var(--color-gold, #ffd644));
}

/* ── Body animations ──────────────────────────────────── */
.robot-sprite.thinking .head-group {
  transform-box: fill-box;
  transform-origin: bottom center;
  animation: tiltHead 1.5s infinite ease-in-out;
}
@keyframes tiltHead {
  0%,
  100% {
    transform: rotate(-5deg);
  }
  50% {
    transform: rotate(5deg);
  }
}

.robot-sprite.working {
  animation: workBounce 0.4s infinite alternate;
}
@keyframes workBounce {
  from {
    transform: translateY(0);
  }
  to {
    transform: translateY(-4px);
  }
}

.robot-sprite.busy .left-arm {
  transform-box: fill-box;
  transform-origin: top right;
  animation: leftArmSwing 0.5s infinite alternate;
}
.robot-sprite.busy .right-arm {
  transform-box: fill-box;
  transform-origin: top left;
  animation: rightArmSwing 0.5s infinite alternate;
}
@keyframes leftArmSwing {
  from {
    transform: rotate(-22deg);
  }
  to {
    transform: rotate(22deg);
  }
}
@keyframes rightArmSwing {
  from {
    transform: rotate(22deg);
  }
  to {
    transform: rotate(-22deg);
  }
}

.robot-sprite.error .head-group {
  animation: shake 0.22s infinite;
}
@keyframes shake {
  0%,
  100% {
    transform: translateX(0);
  }
  25% {
    transform: translateX(-3px);
  }
  75% {
    transform: translateX(3px);
  }
}

/* ── Walking ──────────────────────────────────────────── */
.robot-sprite.walking {
  animation: walkBob 0.36s infinite alternate ease-in-out;
}
@keyframes walkBob {
  from {
    transform: translateY(0px);
  }
  to {
    transform: translateY(-3px);
  }
}

.robot-sprite.walking .left-leg {
  transform-box: fill-box;
  transform-origin: top center;
  animation: legSwingA 0.36s infinite alternate ease-in-out;
}
.robot-sprite.walking .right-leg {
  transform-box: fill-box;
  transform-origin: top center;
  animation: legSwingB 0.36s infinite alternate ease-in-out;
}
@keyframes legSwingA {
  from {
    transform: translateX(-3px) rotate(-10deg);
  }
  to {
    transform: translateX(3px) rotate(10deg);
  }
}
@keyframes legSwingB {
  from {
    transform: translateX(3px) rotate(10deg);
  }
  to {
    transform: translateX(-3px) rotate(-10deg);
  }
}

.robot-sprite.walking .left-arm {
  transform-box: fill-box;
  transform-origin: top right;
  animation: rightArmSwing 0.36s infinite alternate ease-in-out;
}
.robot-sprite.walking .right-arm {
  transform-box: fill-box;
  transform-origin: top left;
  animation: leftArmSwing 0.36s infinite alternate ease-in-out;
}
</style>
