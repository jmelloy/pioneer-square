<template>
  <div class="session-card" @click="$emit('open')">
    <div class="card-top">
      <span class="card-id">{{ guild.id }}</span>
      <span class="card-agents">
        <span class="agent-dot" :class="(guild.agent_count ?? 0) > 0 ? 'active' : 'empty'"></span>
        {{ guild.agent_count || 0 }} agent{{ guild.agent_count !== 1 ? 's' : '' }}
      </span>
    </div>
    <div class="card-name">{{ guild.name }}</div>
    <div class="card-time">Created {{ formatRelative(guild.created_at) }}</div>
    <div class="card-enter">ENTER →</div>
  </div>
</template>

<script setup lang="ts">
import { formatRelative } from '../../utils/format'
import type { Guild } from '../../types'

defineProps<{ guild: Guild }>()
defineEmits<{ (e: 'open'): void }>()
</script>

<style scoped>
.session-card {
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
  align-items: center;
  justify-content: space-between;
}

.card-id {
  font-family: var(--font-pixel);
  font-size: 8px;
  color: var(--color-brass);
}

.card-agents {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 10px;
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
  font-size: 14px;
  color: var(--color-text);
  font-weight: bold;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.card-time {
  font-size: 10px;
  color: var(--color-text-dim);
}

.card-enter {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-teal);
  opacity: 0;
  transition: opacity 0.15s;
  align-self: flex-end;
  margin-top: 4px;
}

.session-card:hover .card-enter {
  opacity: 1;
}
</style>
