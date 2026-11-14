<template>
  <section class="base-card">
    <header v-if="title" class="card-header">
      <span v-if="icon" class="card-icon" aria-hidden="true">{{ icon }}</span>
      <h2 class="card-title">{{ title }}</h2>
      <span v-if="count !== undefined" class="card-count">{{ count }}</span>
      <div class="card-actions"><slot name="actions" /></div>
    </header>
    <div class="card-body"><slot /></div>
    <footer v-if="$slots.footer" class="card-footer"><slot name="footer" /></footer>
  </section>
</template>

<script setup lang="ts">
defineProps<{ title?: string; icon?: string; count?: number }>()
</script>

<style scoped>
/* Gold-rimmed inset panel used for every dashboard section. */
.base-card {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  background: rgba(18, 9, 0, 0.82);
  border: 2px solid var(--color-brass-dark);
  border-radius: 6px;
  box-shadow:
    inset 0 0 0 1px rgba(255, 214, 68, 0.12),
    0 2px 8px rgba(0, 0, 0, 0.5);
}
.card-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--color-brass-dark);
}
.card-icon {
  color: var(--color-brass);
  font-size: 16px;
}
.card-title {
  font-family: var(--font-mono);
  font-size: 18px;
  font-weight: bold;
  color: var(--color-brass-light);
}
.card-count {
  min-width: 22px;
  padding: 0 6px;
  border: 1px solid var(--color-brass-dark);
  border-radius: 3px;
  background: var(--color-bg-tertiary);
  color: var(--color-brass-light);
  font-size: 13px;
  text-align: center;
}
.card-actions {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
}
.card-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
}
.card-footer {
  padding: 8px 14px;
  border-top: 1px solid var(--color-brass-dark);
  color: var(--color-text-dim);
  font-size: 12px;
}
</style>
