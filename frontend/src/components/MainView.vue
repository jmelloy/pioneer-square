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
      <!-- Task tabs — shown for active/recent tasks -->
      <button
        v-for="task in visibleTaskTabs"
        :key="'task-' + task.id"
        class="tab task-tab"
        :class="{ active: activeTab === 'task-' + task.id }"
        @click="activeTab = 'task-' + task.id"
      >
        <span class="task-dot" :class="'task-dot-' + task.state.replace(/[^a-z]/g, '-')"></span>
        <span class="tab-label">{{ task.name || task.id }}</span>
      </button>
    </div>
    <div class="tab-content">
      <FactoryFloor v-if="activeTab === 'factory'" />
      <TaskPane v-else-if="activeTab.startsWith('task-')" :taskId="activeTab.slice(5)" />
      <TerminalPane v-else :agentId="activeTab" />
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { useAgentsStore } from '../stores/agents.js'
import { useTasksStore } from '../stores/tasks.js'
import FactoryFloor from './FactoryFloor.vue'
import TerminalPane from './TerminalPane.vue'
import TaskPane from './TaskPane.vue'

const agentsStore = useAgentsStore()
const tasksStore = useTasksStore()
const agents = computed(() => agentsStore.agents)
const activeTab = ref('factory')

// Show tabs for tasks that are active or recently completed (last 8)
const visibleTaskTabs = computed(() => {
  const active = tasksStore.tasks.filter(t =>
    ['pending', 'planning', 'working', 'awaiting-review', 'followup'].includes(t.state)
  )
  const done = tasksStore.tasks
    .filter(t => t.state === 'done' || t.state === 'failed')
    .slice(0, Math.max(0, 8 - active.length))
  return [...active, ...done].slice(0, 8)
})

// Auto-open task tab when a new task becomes active
watch(() => tasksStore.selectedTaskId, (id) => {
  if (id) activeTab.value = 'task-' + id
})
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
  background: var(--color-bg-secondary);
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
  background: rgba(232, 170, 0, 0.07);
  color: var(--color-text);
}

.tab.active {
  background: var(--color-bg);
  color: var(--color-brass-light);
  border-bottom: 2px solid var(--color-brass);
  margin-bottom: -2px;
  box-shadow: inset 0 -2px 6px rgba(232, 170, 0, 0.1);
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

.task-tab { border-left: 1px solid rgba(255,204,0,0.2); }

.task-dot {
  width: 8px;
  height: 8px;
  border-radius: 2px;
  display: inline-block;
  flex-shrink: 0;
}
.task-dot-pending { background: var(--color-text-dim); }
.task-dot-planning { background: var(--color-blue); animation: dotPulse 1s infinite; }
.task-dot-working { background: var(--color-green); animation: dotPulse 0.5s infinite; }
.task-dot-awaiting-review { background: var(--color-amber); animation: dotPulse 1.5s infinite; }
.task-dot-done { background: var(--color-teal); }
.task-dot-failed { background: var(--color-red); }
.task-dot-follow-up { background: var(--color-orange); animation: dotPulse 0.8s infinite; }

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
