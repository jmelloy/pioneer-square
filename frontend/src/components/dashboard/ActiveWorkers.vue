<template>
  <p v-if="!agents.length" class="empty">No workers</p>
  <div v-else-if="compact" class="worker-bar">
    <StatusDots v-for="g in groups" :key="g.id" :name="g.name" :agents="g.agents" />
  </div>
  <ul v-else class="worker-groups">
    <li v-for="g in groups" :key="g.id">
      <details class="worker-group" open>
        <summary class="worker-summary">
          <span class="worker-name">{{ g.name }}</span>
          <StatusDots class="summary-dots" :name="g.name" :agents="g.agents" />
        </summary>
        <ul class="worker-rows">
          <li v-for="agent in g.agents" :key="agent.id">
            <button class="worker-row" @click="emit('select-agent', agent.id)">
              <span class="dot" :class="'dot-' + agent.state" aria-hidden="true"></span>
              <span class="name">{{ agent.name }}</span>
              <span class="activity">{{ lastLine(agent) }}</span>
              <span class="state" :class="'state-' + agent.state">{{ agent.state }}</span>
            </button>
          </li>
        </ul>
      </details>
    </li>
  </ul>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { isVisibleLogEntry } from '../../utils/logs'
import type { Agent } from '../../types'
import StatusDots from './StatusDots.vue'

// compact: just one row of status dots per worker (the collapsed section bar).
const props = defineProps<{ agents: Agent[]; compact?: boolean }>()
const emit = defineEmits<{ 'select-agent': [id: string] }>()

const groups = computed(() => {
  const byWorker = new Map<string, { id: string; name: string; agents: Agent[] }>()
  for (const agent of props.agents) {
    const id = agent.workerId ?? ''
    let g = byWorker.get(id)
    if (!g) byWorker.set(id, (g = { id, name: agent.workerName || id || 'Unassigned', agents: [] }))
    g.agents.push(agent)
  }
  return [...byWorker.values()]
})

function lastLine(agent: Agent): string {
  return agent.logs.filter(isVisibleLogEntry).at(-1)?.line ?? ''
}
</script>

<style scoped>
.worker-groups,
.worker-rows {
  list-style: none;
}
.worker-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
}
.worker-summary {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 14px;
  background: var(--color-bg-tertiary);
  color: var(--color-brass-light);
  font-family: var(--font-mono);
  font-size: 13px;
  list-style: none;
  cursor: pointer;
}
.worker-summary::before {
  content: '▸';
  color: var(--color-brass);
}
.worker-group[open] > .worker-summary::before {
  content: '▾';
}
.worker-summary::-webkit-details-marker {
  display: none;
}
/* Dots replace the rows only while the worker item is collapsed. */
.worker-group[open] .summary-dots {
  display: none;
}
.worker-name {
  font-weight: bold;
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
