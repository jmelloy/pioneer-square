<template>
  <div class="ticker-tape">
    <div class="ticker-content">
      <span v-for="(msg, i) in messages" :key="i" class="ticker-msg"> ⚙ {{ msg }} </span>
    </div>
  </div>
</template>

<script setup lang="ts">
defineProps<{ messages: string[] }>()
</script>

<style scoped>
.ticker-tape {
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  height: 28px;
  background: var(--color-bg-secondary);
  border-top: 2px solid var(--color-brass-dark);
  overflow: hidden;
  display: flex;
  align-items: center;
  z-index: 8;
}

.ticker-content {
  display: flex;
  animation: tickerScroll 22s linear infinite;
  white-space: nowrap;
}

.ticker-msg {
  font-size: 8px;
  color: var(--color-amber);
  margin-right: 60px;
  letter-spacing: 1px;
  text-shadow: 0 0 4px rgba(255, 204, 0, 0.5);
}
.ticker-msg:nth-child(3n + 1) {
  color: var(--color-amber);
}
.ticker-msg:nth-child(3n + 2) {
  color: var(--color-teal);
  text-shadow: 0 0 4px rgba(0, 187, 170, 0.5);
}
.ticker-msg:nth-child(3n) {
  color: var(--color-gold);
  text-shadow: 0 0 4px rgba(255, 214, 68, 0.5);
}

@keyframes tickerScroll {
  from {
    transform: translateX(100vw);
  }
  to {
    transform: translateX(-100%);
  }
}
</style>
