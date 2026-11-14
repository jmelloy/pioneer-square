<template>
  <p v-if="!agents.length" class="empty">No workers</p>
  <ul v-else class="worker-rows">
    <li v-for="agent in agents" :key="agent.id">
      <button class="worker-row" @click="emit('select-agent', agent.id)">
        <span class="dot" :class="'dot-' + agent.state" aria-hidden="true"></span>
        <span class="name">{{ agent.name }}</span>
        <span class="activity">{{ lastLine(agent) }}</span>
        <span class="state" :class="'state-' + agent.state">{{ agent.state }}</span>
      </button>
    </li>
  </ul>
</template>

<script setup lang="ts">
import { isVisibleLogEntry } from '../../utils/logs'
import type { Agent } from '../../types'

defineProps<{ agents: Agent[] }>()
const emit = defineEmits<{ 'select-agent': [id: string] }>()

function lastLine(agent: Agent): string {
  return agent.logs.filter(isVisibleLogEntry).at(-1)?.line ?? ''
}
</script>

<style scoped>
.worker-rows {
  list-style: none;
}
.worker-row {
  display: grid;
  grid-template-columns: 12px auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 10px 14px;
  background: none;
  border: none;
  border-bottom: 1px solid var(--color-bg-tertiary);
  color: var(--color-text);
  font-family: var(--font-mono);
  font-size: 14px;
  text-align: left;
  cursor: pointer;
}
li:last-child .worker-row {
  border-bottom: none;
}
.worker-row:hover {
  background: rgba(232, 170, 0, 0.07);
}
.name {
  font-weight: bold;
  white-space: nowrap;
}
.activity {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--color-text-dim);
  font-size: 13px;
}
.state {
  font-size: 12px;
  color: var(--color-text-dim);
  text-transform: uppercase;
}
.dot {
  width: 12px;
  height: 12px;
  border-radius: 50%;
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
.state-working {
  color: var(--color-green);
}
.state-thinking {
  color: var(--color-blue);
}
.state-busy {
  color: var(--color-orange);
}
.state-error {
  color: var(--color-red);
}
.empty {
  padding: 14px;
  color: var(--color-text-dim);
  font-size: 13px;
}
</style>
