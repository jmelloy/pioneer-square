<template>
  <div class="main-view">
    <div class="tab-bar">
      <button
        class="tab"
        :class="{ active: activeTab === 'factory' }"
        @click="activeTab = 'factory'"
      >
        <span class="tab-icon">⚙</span>
        <span class="tab-label">Factory Floor</span>
      </button>
      <button
        v-for="agent in agents"
        :key="agent.id"
        class="tab agent-tab"
        :class="{ active: activeTab === agent.id }"
        @click="activeTab = agent.id"
      >
        <span class="state-dot" :class="agent.state"></span>
        <span class="tab-label">{{ agent.name }}</span>
      </button>
    </div>
    <div class="tab-content">
      <FactoryFloor v-if="activeTab === 'factory'" />
      <TerminalPane v-else :agentId="activeTab" />
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useAgentsStore } from '../stores/agents.js'
import FactoryFloor from './FactoryFloor.vue'
import TerminalPane from './TerminalPane.vue'

const agentsStore = useAgentsStore()
const agents = computed(() => agentsStore.agents)
const activeTab = ref('factory')
</script>

<style scoped>
.main-view {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--color-bg);
}

.tab-bar {
  display: flex;
  background: linear-gradient(180deg, var(--color-bg-secondary), var(--color-bg));
  border-bottom: 2px solid var(--color-brass-dark);
  overflow-x: auto;
  flex-shrink: 0;
}

.tab {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 16px;
  background: none;
  border: none;
  border-right: 1px solid var(--color-bg-tertiary);
  color: var(--color-text-dim);
  cursor: pointer;
  font-family: var(--font-mono);
  font-size: 12px;
  white-space: nowrap;
  transition: all 0.15s;
}

.tab:hover {
  background: rgba(192, 122, 255, 0.08);
  color: var(--color-brass-light);
}

.tab.active {
  background: rgba(192, 122, 255, 0.12);
  color: var(--color-brass-light);
  border-bottom: 2px solid var(--color-brass);
  margin-bottom: -2px;
  box-shadow: inset 0 -2px 8px rgba(192, 122, 255, 0.15);
}

.tab-icon {
  font-size: 14px;
}

.tab-label {
  font-size: 11px;
}

.state-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
  flex-shrink: 0;
}

.state-dot.idle { background: var(--color-text-dim); }
.state-dot.thinking { background: var(--color-blue); animation: dotPulse 1s infinite; }
.state-dot.working { background: var(--color-green); animation: dotPulse 0.5s infinite; }
.state-dot.busy { background: var(--color-orange); animation: dotPulse 0.8s infinite; }
.state-dot.error { background: var(--color-red); }

@keyframes dotPulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.5; transform: scale(0.7); }
}

.tab-content {
  flex: 1;
  overflow: hidden;
  position: relative;
}
</style>
