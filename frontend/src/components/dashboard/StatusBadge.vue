<template>
  <span class="status-badge" :class="'badge-' + color">{{ label }}</span>
</template>

<script setup lang="ts">
// `color` is a store color token: green | teal | amber | blue | red | orange | dim.
defineProps<{ label: string; color: string }>()
</script>

<style scoped>
.status-badge {
  display: inline-block;
  padding: 2px 8px;
  border: 1px solid currentColor;
  border-radius: 4px;
  background: rgba(0, 0, 0, 0.35);
  color: var(--color-text-dim);
  font-family: var(--font-mono);
  font-size: 12px;
  font-weight: bold;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  white-space: nowrap;
}
.badge-green {
  color: var(--color-green);
  background: rgba(51, 221, 136, 0.12);
}
.badge-teal {
  color: var(--color-teal);
  background: rgba(0, 187, 170, 0.12);
}
.badge-amber {
  color: var(--color-amber);
  background: rgba(255, 204, 0, 0.12);
}
.badge-blue {
  color: var(--color-blue);
  background: rgba(68, 170, 238, 0.12);
}
.badge-red {
  color: var(--color-red);
  background: rgba(238, 51, 34, 0.12);
}
.badge-orange {
  color: var(--color-orange);
  background: rgba(255, 119, 0, 0.12);
}
</style>
