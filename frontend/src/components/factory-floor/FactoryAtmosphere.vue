<template>
  <div class="floor-grid"></div>

  <div class="sparkle-field">
    <div
      class="sparkle"
      v-for="n in 6"
      :key="`sp${n}`"
      :style="`left: ${(n * 79 + 11) % 92}%; top: ${(n * 53 + 9) % 88}%; font-size: ${
        9 + (n % 2) * 3
      }px; animation-delay: ${((n * 0.41) % 2.6).toFixed(1)}s; animation-duration: ${
        3 + (n % 3) * 0.6
      }s;`"
    >
      {{ ['✦', '★', '✧', '⋆'][n % 4] }}
    </div>
  </div>

  <div class="ceiling-pipe"></div>
  <div class="pipe-joint j1"></div>
  <div class="pipe-joint j2"></div>

  <div class="steam-vent vent1">
    <div class="steam-particle" v-for="n in 3" :key="n" :style="`--delay: ${n * 0.4}s`"></div>
  </div>
  <div class="steam-vent vent2">
    <div class="steam-particle" v-for="n in 3" :key="n" :style="`--delay: ${n * 0.5}s`"></div>
  </div>

  <div class="ambient-gear g1">⚙</div>
  <div class="ambient-gear g2">⚙</div>
</template>

<script setup lang="ts"></script>

<style scoped>
.floor-grid {
  position: absolute;
  inset: 0;
  background-image:
    linear-gradient(rgba(232, 170, 0, 0.04) 1px, transparent 1px),
    linear-gradient(90deg, rgba(232, 170, 0, 0.04) 1px, transparent 1px);
  background-size: 48px 48px;
  pointer-events: none;
}

.sparkle-field {
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 1;
}
.sparkle {
  position: absolute;
  animation: sparkleFade 3s infinite ease-in-out;
}
.sparkle:nth-child(4n + 1) {
  color: var(--color-gold);
}
.sparkle:nth-child(4n + 2) {
  color: var(--color-teal);
}
.sparkle:nth-child(4n + 3) {
  color: var(--color-amber);
}
.sparkle:nth-child(4n) {
  color: var(--color-cream);
}

@keyframes sparkleFade {
  0%,
  100% {
    opacity: 0;
    transform: scale(0.3) rotate(0deg);
  }
  40%,
  60% {
    opacity: 0.75;
    transform: scale(1.05) rotate(180deg);
  }
}

.ceiling-pipe {
  position: absolute;
  top: 18px;
  left: 0;
  right: 0;
  height: 10px;
  background: linear-gradient(180deg, #6e3500 0%, #b34a00 35%, #d76420 55%, #7a3c00 100%);
  border-top: 1px solid #5a2a00;
  border-bottom: 1px solid #5a2a00;
  box-shadow: 0 0 6px rgba(204, 85, 0, 0.4);
  z-index: 2;
}

.pipe-joint {
  position: absolute;
  top: 14px;
  width: 18px;
  height: 18px;
  background: radial-gradient(
    circle,
    var(--color-brass-light) 20%,
    var(--color-brass) 60%,
    var(--color-brass-dark) 100%
  );
  border: 2px solid var(--color-brass-dark);
  border-radius: 50%;
  box-shadow: 0 0 5px rgba(232, 170, 0, 0.5);
  z-index: 3;
}
.j1 {
  left: 22%;
}
.j2 {
  left: 64%;
}

.steam-vent {
  position: absolute;
  width: 12px;
  z-index: 2;
}
.vent1 {
  top: 30px;
  left: calc(22% + 5px);
}
.vent2 {
  top: 30px;
  left: calc(64% + 5px);
}

.steam-particle {
  position: absolute;
  width: 8px;
  height: 8px;
  background: radial-gradient(circle, rgba(255, 232, 176, 0.7) 0%, transparent 70%);
  border-radius: 50%;
  animation: steamRise 2.4s var(--delay, 0s) infinite ease-out;
}

@keyframes steamRise {
  0% {
    transform: translateY(0) scale(0.4);
    opacity: 0.75;
  }
  60% {
    transform: translateY(-22px) scale(1.4) translateX(4px);
    opacity: 0.4;
  }
  100% {
    transform: translateY(-44px) scale(1.9) translateX(-4px);
    opacity: 0;
  }
}

.ambient-gear {
  position: absolute;
  user-select: none;
  pointer-events: none;
  line-height: 1;
}
.ambient-gear.g1 {
  right: 26px;
  top: 56px;
  font-size: 48px;
  color: var(--color-gold);
  text-shadow:
    0 0 10px var(--color-gold),
    0 0 18px rgba(255, 214, 68, 0.35);
  animation: gearSpin 9s linear infinite;
}
.ambient-gear.g2 {
  right: 70px;
  top: 90px;
  font-size: 30px;
  color: var(--color-teal);
  text-shadow:
    0 0 8px var(--color-teal),
    0 0 16px rgba(0, 187, 170, 0.3);
  animation: gearSpin 5s linear infinite reverse;
}

@keyframes gearSpin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}
</style>
