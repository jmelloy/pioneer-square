<template>
  <button type="button" class="session-card" @click="$emit('open')">
    <span class="card-top">
      <span class="card-id">{{ guild.id }}</span>
      <span class="card-agents">
        <span class="agent-dot" :class="(guild.agent_count ?? 0) > 0 ? 'active' : 'empty'"></span>
        {{ guild.agent_count || 0 }} agent{{ guild.agent_count !== 1 ? 's' : '' }}
      </span>
    </span>
    <span class="card-name">{{ guild.name }}</span>
    <span class="card-time">Created {{ formatRelative(guild.created_at) }}</span>
    <span class="card-enter">ENTER →</span>
  </button>
</template>

<script setup lang="ts">
import { formatRelative } from '../../utils/format'
import type { Guild } from '../../types'

defineProps<{ guild: Guild }>()
defineEmits<{ (e: 'open'): void }>()
</script>

<style scoped>
.session-card {
  font: inherit;
  text-align: left;
  width: 100%;
  min-width: 0;
  background: var(--color-bg-secondary);
  border: 2px solid var(--color-brass-dark);
  padding: 16px 18px;
  cursor: pointer;
  transition: all 0.15s;
  display: flex;
  flex-direction: column;
  gap: 6px;
  position: relative;
}

.session-card:hover {
  border-color: var(--color-brass);
  background: var(--color-bg-tertiary);
  box-shadow: 0 0 16px rgba(232, 170, 0, 0.2);
}

.card-top {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  justify-content: space-between;
}

.card-id {
  font: var(--text-caption) / 1.4 var(--font-mono);
  color: var(--color-brass);
}

.card-agents {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: var(--text-caption);
  font-variant-numeric: tabular-nums;
  color: var(--color-text-dim);
}

.agent-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--color-text-dim);
}

.agent-dot.active {
  background: var(--color-green);
  box-shadow: 0 0 6px var(--color-green);
  animation: pulse 2s infinite;
}

@keyframes pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.4;
  }
}

.card-name {
  font: 600 var(--text-card-title) / var(--leading-heading) var(--font-ui);
  color: var(--color-text);
  overflow-wrap: anywhere;
}

.card-time {
  font-size: var(--text-caption);
  color: var(--color-text-dim);
}

.card-enter {
  font: 600 var(--text-small) / 1.4 var(--font-ui);
  color: var(--color-teal);
  align-self: flex-end;
  margin-top: 4px;
}
</style>
