<template>
  <BaseCard title="Workers" :count="workers.length">
    <div class="stats">
      <div v-for="s in summary" :key="s.state" class="stat" :class="'stat-' + s.state">
        <span class="stat-value">{{ s.n }}</span>
        <span class="stat-label">{{ s.state }}</span>
      </div>
    </div>
    <p v-if="!agents.length" class="empty">No agents online</p>
    <ul v-else class="rows">
      <li v-for="agent in agents" :key="agent.id">
        <button class="row" @click="emit('select-agent', agent.id)">
          <span class="dot" :class="'dot-' + agent.state"></span>
          <span class="name">{{ agent.name }}</span>
          <span class="meta">{{ agent.workerName || agent.workerId || '—' }}</span>
          <span class="state">{{ agent.state }}</span>
        </button>
      </li>
    </ul>
  </BaseCard>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import BaseCard from './BaseCard.vue'
import type { Agent, Worker } from '../types'

const props = defineProps<{ agents: Agent[]; workers: Worker[] }>()
const emit = defineEmits<{ 'select-agent': [id: string] }>()

const SHOWN = ['working', 'thinking', 'idle', 'error'] as const
const summary = computed(() =>
  SHOWN.map((state) => ({ state, n: props.agents.filter((a) => a.state === state).length })),
)
</script>

<style scoped>
.stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 6px;
  padding: 10px 12px;
}
.stat {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 6px 0;
  background: var(--color-bg);
  border: 1px solid var(--color-bg-tertiary);
}
.stat-value {
  font-family: var(--font-pixel);
  font-size: 14px;
}
.stat-label {
  font-size: 10px;
  color: var(--color-text-dim);
  text-transform: uppercase;
}
.stat-working .stat-value {
  color: var(--color-green);
}
.stat-thinking .stat-value {
  color: var(--color-blue);
}
.stat-idle .stat-value {
  color: var(--color-text-dim);
}
.stat-error .stat-value {
  color: var(--color-red);
}
.rows {
  list-style: none;
}
.row {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 6px 12px;
  background: none;
  border: none;
  border-top: 1px solid var(--color-bg-tertiary);
  color: var(--color-text);
  font-family: var(--font-mono);
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}
.row:hover {
  background: rgba(232, 170, 0, 0.07);
}
.name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.meta,
.state {
  font-size: 11px;
  color: var(--color-text-dim);
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
  background: var(--color-text-dim);
}
.dot-working {
  background: var(--color-green);
}
.dot-thinking {
  background: var(--color-blue);
}
.dot-busy {
  background: var(--color-orange);
}
.dot-error {
  background: var(--color-red);
}
.empty {
  padding: 12px;
  font-size: 12px;
  color: var(--color-text-dim);
}
</style>
