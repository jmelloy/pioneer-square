<template>
  <section class="base-card">
    <header v-if="title || $slots.actions" class="base-card-header">
      <h2 class="base-card-title">{{ title }}</h2>
      <span v-if="count !== undefined" class="base-card-count">{{ count }}</span>
      <div class="base-card-actions"><slot name="actions" /></div>
    </header>
    <div class="base-card-body"><slot /></div>
    <footer v-if="$slots.footer" class="base-card-footer"><slot name="footer" /></footer>
  </section>
</template>

<script setup lang="ts">
defineProps<{ title?: string; count?: number }>()
</script>

<style scoped>
.base-card {
  display: flex;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
  background: var(--color-bg-secondary);
  border: var(--border-brass);
  box-shadow: var(--shadow-brass);
}
.base-card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--color-brass-dark);
  background: var(--color-bg-tertiary);
}
.base-card-title {
  font-family: var(--font-pixel);
  font-size: 9px;
  font-weight: normal;
  color: var(--color-brass-light);
  text-transform: uppercase;
  letter-spacing: 1px;
}
.base-card-count {
  font-size: 11px;
  color: var(--color-text-dim);
}
.base-card-actions {
  margin-left: auto;
  display: flex;
  gap: 6px;
}
.base-card-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
}
.base-card-footer {
  padding: 6px 12px;
  border-top: 1px solid var(--color-brass-dark);
  font-size: 11px;
  color: var(--color-text-dim);
}
</style>
