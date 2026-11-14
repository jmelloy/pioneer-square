<template>
  <span class="status-dots" :title="tooltip" role="img" :aria-label="tooltip">
    <span
      v-for="agent in agents"
      :key="agent.id"
      class="slot"
      :class="'slot-' + tone(agent.state)"
    ></span>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { Agent, AgentState } from '../../types'

// One dot per agent slot: idle green, working amber, error red, offline dim.
const props = defineProps<{ name: string; agents: Agent[] }>()

const tooltip = computed(
  () => `${props.name}\n` + props.agents.map((a) => `${a.name}: ${a.state}`).join('\n'),
)

function tone(state: AgentState): string {
  if (state === 'idle' || state === 'error' || state === 'offline') return state
  return 'working' // thinking / working / busy
}
</script>

<style scoped>
.status-dots {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  cursor: help;
}
.slot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--color-text-dim);
}
.slot-idle {
  background: var(--color-green);
}
.slot-working {
  background: var(--color-amber);
}
.slot-error {
  background: var(--color-red);
}
</style>
