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
      <!-- Crown (foreman only) — three prongs with square gem accents -->
      <g v-if="type === 'foreman'" class="crown">
        <rect x="9" y="3" width="22" height="2" fill="var(--color-gold, #ffd644)" />
        <rect x="12" y="-1" width="4" height="5" fill="var(--color-gold, #ffd644)" />
        <rect x="18" y="-4" width="4" height="8" fill="var(--color-gold, #ffd644)" />
        <rect x="24" y="-1" width="4" height="5" fill="var(--color-gold, #ffd644)" />
        <rect x="13" y="-1" width="2" height="2" fill="var(--color-red, #ee3322)" />
        <rect x="19" y="-4" width="2" height="2" fill="var(--color-blue, #44aaee)" />
        <rect x="25" y="-1" width="2" height="2" fill="var(--color-green, #88dd22)" />
      </g>

      <!-- Antenna (workers) — square ball -->
      <template v-if="type !== 'foreman'">
        <rect x="19" y="1" width="2" height="3" fill="var(--agent-color)" />
        <rect class="antenna-ball" x="18" y="-2" width="4" height="4" fill="var(--agent-color)" />
      </template>

      <!-- Head box -->
      <rect
        class="head-box"
        x="9"
        y="5"
        width="22"
        height="18"
        fill="rgba(18,9,0,0.93)"
        stroke="var(--agent-color)"
        stroke-width="2"
      />

      <!-- Eyes -->
      <rect class="eye" x="12" y="10" width="6" height="5" />
      <rect class="eye" x="22" y="10" width="6" height="5" />

      <!-- Ear bolts (square dots) -->
      <rect x="9" y="13" width="2" height="2" fill="var(--agent-color)" opacity="0.75" />
      <rect x="29" y="13" width="2" height="2" fill="var(--agent-color)" opacity="0.75" />

      <!-- Speaker grill -->
      <rect x="13" y="20" width="3" height="1" fill="var(--agent-color)" opacity="0.65" />
      <rect x="18" y="20" width="3" height="1" fill="var(--agent-color)" opacity="0.65" />
      <rect x="23" y="20" width="3" height="1" fill="var(--agent-color)" opacity="0.65" />
    </g>

    <!-- Neck -->
    <rect x="16" y="23" width="8" height="3" fill="var(--agent-color)" opacity="0.45" />

    <!-- Body -->
    <rect
      x="7"
      y="26"
      width="26"
      height="18"
      fill="rgba(18,9,0,0.93)"
      stroke="var(--agent-color)"
      stroke-width="2"
    />
    <!-- Chest panel recess -->
    <rect
      x="13"
      y="29"
      width="14"
      height="12"
      fill="rgba(0,0,0,0.65)"
      stroke="var(--agent-color)"
      stroke-width="1"
    />
    <!-- LED indicator — square pixel LED -->
    <rect class="led" x="17" y="32" width="6" height="6" />
    <!-- Shoulder rivets — square pixels -->
    <rect x="8" y="27" width="2" height="2" fill="var(--agent-color)" opacity="0.85" />
    <rect x="30" y="27" width="2" height="2" fill="var(--agent-color)" opacity="0.85" />
    <!-- Side exhaust vents -->
    <rect x="29" y="33" width="3" height="1" fill="var(--agent-color)" opacity="0.55" />
    <rect x="29" y="36" width="3" height="1" fill="var(--agent-color)" opacity="0.55" />
    <rect x="29" y="39" width="3" height="1" fill="var(--agent-color)" opacity="0.55" />
    <!-- Belt line -->
    <rect
      x="7"
      y="42"
      width="26"
      height="2"
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
        fill="rgba(18,9,0,0.93)"
        stroke="var(--agent-color)"
        stroke-width="2"
      />
      <rect x="3" y="30" width="2" height="2" fill="var(--agent-color)" opacity="0.7" />
      <rect
        x="0"
        y="38"
        width="8"
        height="5"
        fill="rgba(18,9,0,0.93)"
        stroke="var(--agent-color)"
        stroke-width="2"
      />
    </g>

    <!-- Right arm -->
    <g class="right-arm">
      <rect
        x="33"
        y="27"
        width="6"
        height="12"
        fill="rgba(18,9,0,0.93)"
        stroke="var(--agent-color)"
        stroke-width="2"
      />
      <rect x="35" y="30" width="2" height="2" fill="var(--agent-color)" opacity="0.7" />
      <rect
        x="32"
        y="38"
        width="8"
        height="5"
        fill="rgba(18,9,0,0.93)"
        stroke="var(--agent-color)"
        stroke-width="2"
      />
    </g>

    <!-- Left leg + foot -->
    <g class="left-leg">
      <rect
        x="10"
        y="44"
        width="9"
        height="10"
        fill="rgba(18,9,0,0.93)"
        stroke="var(--agent-color)"
        stroke-width="2"
      />
      <rect
        x="8"
        y="51"
        width="13"
        height="5"
        fill="rgba(18,9,0,0.93)"
        stroke="var(--agent-color)"
        stroke-width="2"
      />
    </g>
    <!-- Right leg + foot -->
    <g class="right-leg">
      <rect
        x="21"
        y="44"
        width="9"
        height="10"
        fill="rgba(18,9,0,0.93)"
        stroke="var(--agent-color)"
        stroke-width="2"
      />
      <rect
        x="19"
        y="51"
        width="13"
        height="5"
        fill="rgba(18,9,0,0.93)"
        stroke="var(--agent-color)"
        stroke-width="2"
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
  /* Stepped pulse — toggles between two sizes, no smooth scale */
  animation: ledPulse 0.5s steps(1, end) infinite alternate;
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
  animation: antennaPulse 1.5s steps(1, end) infinite alternate;
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
  /* Stepped head tilt — 3-frame cycle via 3 distinct keyframes + steps(1) */
  animation: tiltHead 1.2s steps(1, end) infinite;
}
@keyframes tiltHead {
  0%,
  33% {
    transform: rotate(-4deg);
  }
  34%,
  66% {
    transform: rotate(0deg);
  }
  67%,
  100% {
    transform: rotate(4deg);
  }
}

.robot-sprite.working {
  /* Bounce snaps between two y positions — not smooth */
  animation: workBounce 0.4s steps(1, end) infinite alternate;
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
  animation: leftArmSwing 0.5s steps(1, end) infinite alternate;
}
.robot-sprite.busy .right-arm {
  transform-box: fill-box;
  transform-origin: top left;
  animation: rightArmSwing 0.5s steps(1, end) infinite alternate;
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

/* ── Walking — 2-frame stepped cycle, sprite-sheet feel ── */
.robot-sprite.walking {
  /* Hop snaps between 2 vertical positions, no tween */
  animation: walkBob 0.32s steps(1, end) infinite alternate;
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
  animation: legSwingA 0.32s steps(1, end) infinite alternate;
}
.robot-sprite.walking .right-leg {
  transform-box: fill-box;
  transform-origin: top center;
  animation: legSwingB 0.32s steps(1, end) infinite alternate;
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
  animation: rightArmSwing 0.32s steps(1, end) infinite alternate;
}
.robot-sprite.walking .right-arm {
  transform-box: fill-box;
  transform-origin: top left;
  animation: leftArmSwing 0.32s steps(1, end) infinite alternate;
}
</style>
