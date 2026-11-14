<template>
  <div class="pill-tabs" role="tablist">
    <button
      v-for="tab in tabs"
      :key="tab.id"
      class="pill-tab"
      role="tab"
      :aria-selected="tab.id === modelValue"
      :class="{ active: tab.id === modelValue }"
      @click="emit('update:modelValue', tab.id)"
    >
      <span v-if="tab.icon" aria-hidden="true">{{ tab.icon }}</span>
      {{ tab.label }}<template v-if="tab.count !== undefined"> ({{ tab.count }})</template>
    </button>
  </div>
</template>

<script setup lang="ts">
export interface PillTab {
  id: string
  label: string
  icon?: string
  count?: number
}

defineProps<{ tabs: PillTab[]; modelValue: string }>()
const emit = defineEmits<{ 'update:modelValue': [id: string] }>()
</script>

<style scoped>
.pill-tabs {
  display: flex;
  gap: 6px;
  overflow-x: auto;
  flex-shrink: 0;
}
.pill-tab {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 16px;
  border: 2px solid var(--color-brass-dark);
  border-radius: 5px;
  background: var(--color-bg-secondary);
  color: var(--color-text);
  font-family: var(--font-mono);
  font-size: 14px;
  white-space: nowrap;
  cursor: pointer;
  transition: background 0.12s;
}
.pill-tab:hover {
  background: var(--color-bg-tertiary);
}
.pill-tab.active {
  background: linear-gradient(180deg, var(--color-brass-light), var(--color-brass));
  border-color: var(--color-brass-light);
  color: var(--color-bg);
  font-weight: bold;
}
</style>
