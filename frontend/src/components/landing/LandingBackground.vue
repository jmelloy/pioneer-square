<template>
  <div class="landing-bg">
    <div class="gear gear-1"></div>
    <div class="gear gear-2"></div>
    <div class="pipe pipe-h pipe-top"></div>
    <div class="pipe pipe-h pipe-bottom"></div>
    <div class="sparkles">
      <span v-for="n in 12" :key="n" class="sparkle" :style="sparkleStyle(n)"></span>
    </div>
  </div>
</template>

<script setup lang="ts">
function sparkleStyle(n: number) {
  const seed = n * 137.508
  return {
    left: `${(seed * 7.3) % 100}%`,
    top: `${(seed * 3.7) % 100}%`,
    animationDelay: `${(n * 0.4) % 3}s`,
    animationDuration: `${2 + (n % 3)}s`,
  }
}
</script>

<style scoped>
.landing-bg {
  position: absolute;
  inset: 0;
  pointer-events: none;
}

.gear {
  position: absolute;
  border-radius: 50%;
  border: 6px solid var(--color-brass-dark);
  opacity: 0.12;
}
.gear::before,
.gear::after {
  content: '';
  position: absolute;
  background: var(--color-brass-dark);
}
.gear-1 {
  width: 260px;
  height: 260px;
  bottom: -60px;
  left: -60px;
  animation: spin 30s linear infinite;
}
.gear-2 {
  width: 180px;
  height: 180px;
  top: -40px;
  right: 120px;
  animation: spin 20s linear infinite reverse;
}

.pipe {
  position: absolute;
  background: linear-gradient(
    180deg,
    var(--color-brass-dark) 0%,
    #6b4f00 50%,
    var(--color-brass-dark) 100%
  );
  opacity: 0.15;
}
.pipe-h {
  height: 18px;
  left: 0;
  right: 0;
}
.pipe-top {
  top: 0;
}
.pipe-bottom {
  bottom: 0;
}

.sparkle {
  position: absolute;
  width: 3px;
  height: 3px;
  background: var(--color-brass-light);
  border-radius: 50%;
  animation: twinkle 2s ease-in-out infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
@keyframes twinkle {
  0%,
  100% {
    opacity: 0.1;
    transform: scale(1);
  }
  50% {
    opacity: 0.9;
    transform: scale(1.6);
  }
}
</style>
