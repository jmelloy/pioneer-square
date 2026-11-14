<template>
  <div class="dashboard">
    <BaseCard title="Factory Floor" class="floor-card">
      <FactoryFloor />
    </BaseCard>
    <LayoutGrid class="dashboard-grid">
      <WorkerStats
        :agents="agentsStore.agents"
        :workers="agentsStore.workers"
        @select-agent="uiStore.selectAgent"
      />
      <TaskList
        :tasks="recentTasks"
        :state-label="tasksStore.stateLabel"
        :state-color="tasksStore.stateColor"
        @select-task="uiStore.openTaskTab"
      />
      <ActivityLog :events="activity" @select-agent="uiStore.selectAgent" />
    </LayoutGrid>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useAgentsStore } from '../stores/agents'
import { useTasksStore } from '../stores/tasks'
import { useUiStore } from '../stores/ui'
import { isVisibleLogEntry } from '../utils/logs'
import BaseCard from './BaseCard.vue'
import LayoutGrid from './LayoutGrid.vue'
import FactoryFloor from './FactoryFloor.vue'
import WorkerStats from './WorkerStats.vue'
import TaskList from './TaskList.vue'
import ActivityLog, { type ActivityEvent } from './ActivityLog.vue'

const agentsStore = useAgentsStore()
const tasksStore = useTasksStore()
const uiStore = useUiStore()

const LIMIT = 50

// Newest first; ISO timestamps sort lexically.
const recentTasks = computed(() =>
  [...tasksStore.liveTasks]
    .sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? ''))
    .slice(0, LIMIT),
)

// ponytail: merges in-memory agent logs on every change; fine for tens of agents.
const activity = computed<ActivityEvent[]>(() =>
  agentsStore.agents
    .flatMap((a) =>
      a.logs
        .filter(isVisibleLogEntry)
        .slice(-LIMIT)
        .map((e) => ({ ...e, agentId: a.id, agentName: a.name })),
    )
    .sort((a, b) => b.timestamp.localeCompare(a.timestamp))
    .slice(0, LIMIT),
)
</script>

<style scoped>
.dashboard {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: auto;
}
.floor-card {
  height: 320px;
  flex-shrink: 0;
  margin: 12px 12px 0;
}
.floor-card :deep(.base-card-body) {
  overflow: hidden;
}
.dashboard-grid {
  flex: 1;
  min-height: 320px;
}
</style>
