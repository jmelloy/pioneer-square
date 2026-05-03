<template>
  <div class="log-pane" :class="'kind-' + kind">
    <PaneHeader :icon="icon" :title-text="titleText" :entity-state="entityState">
      <template v-if="kind === 'task'" #meta>
        <span v-if="taskPhase" class="phase-badge" :class="taskPhase">{{ taskPhase }}</span>
        <span v-if="taskStateLabel" class="state-badge" :class="stateBadgeClass">
          {{ taskStateLabel }}
        </span>
      </template>
    </PaneHeader>

    <TaskHeader
      v-if="kind === 'task'"
      :task-id="task.id"
      :task-branch="task.branch"
      :task-pr-url="task.pr_url"
      :task-created-at="task.created_at"
      :task-description="task.description"
    />

    <LogList ref="logListRef" :logs="logs" />

    <AgentActions v-if="kind === 'agent'" :agent-id="id" :agent-state="entityState" />
    <WorkerActions v-if="kind === 'worker'" :worker-id="id" :worker-state="entityState" />
    <TaskActions
      v-if="kind === 'task'"
      :task-id="id"
      :task-state="task.state"
      :worker-id="task.worker_id"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { useAgentsStore } from '../stores/agents'
import { useTasksStore } from '../stores/tasks'
import { useGuildStore } from '../stores/guild'
import type { LogEntry, Task } from '../types'
import PaneHeader from './log-pane/PaneHeader.vue'
import LogList from './log-pane/LogList.vue'
import AgentActions from './log-pane/AgentActions.vue'
import WorkerActions from './log-pane/WorkerActions.vue'
import TaskHeader from './log-pane/TaskHeader.vue'
import TaskActions from './log-pane/TaskActions.vue'

type PaneKind = 'agent' | 'worker' | 'task'

const props = defineProps<{
  kind: PaneKind
  id: string
}>()

const agentsStore = useAgentsStore()
const tasksStore = useTasksStore()
const guildStore = useGuildStore()

const logListRef = ref<InstanceType<typeof LogList> | null>(null)

const agent = computed(() =>
  props.kind === 'agent' ? agentsStore.agents.find((a) => a.id === props.id) : null,
)
const worker = computed(() =>
  props.kind === 'worker' ? agentsStore.workers.find((w) => w.id === props.id) : null,
)
const task = computed<Partial<Task>>(() =>
  props.kind === 'task'
    ? tasksStore.tasks.find((t) => t.id === props.id) || ({} as Partial<Task>)
    : ({} as Partial<Task>),
)

const logs = computed<LogEntry[]>(() => {
  if (props.kind === 'agent') return agent.value?.logs || []
  if (props.kind === 'worker') return agentsStore.workerLogs[props.id] || []
  return tasksStore.taskLogs[props.id] || []
})

const icon = computed(() => {
  if (props.kind === 'agent') return '▶'
  if (props.kind === 'worker') return '⚙'
  return '◆'
})

const titleText = computed(() => {
  if (props.kind === 'agent') return agent.value?.name || 'Unknown Agent'
  if (props.kind === 'worker') return worker.value?.name || worker.value?.id || props.id
  return task.value.name || task.value.description?.slice(0, 60) || props.id
})

const entityState = computed(() => {
  if (props.kind === 'agent') return agent.value?.state
  if (props.kind === 'worker') return worker.value?.state
  return undefined
})

const taskPhase = computed(() =>
  props.kind === 'task' ? task.value.phase || 'execute' : undefined,
)
const taskStateLabel = computed(() =>
  task.value.state ? tasksStore.stateLabel(task.value.state) : '',
)
const stateBadgeClass = computed(
  () => `state-${(task.value.state || 'pending').replace(/[^a-z]/g, '-')}`,
)

async function loadLogs() {
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  if (props.kind === 'agent') {
    await agentsStore.fetchAgentLogs(guildId, props.id)
  } else if (props.kind === 'worker') {
    await agentsStore.fetchWorkerLogs(guildId, props.id)
  } else if (props.kind === 'task' && !tasksStore.taskLogs[props.id]) {
    await tasksStore.fetchTaskLogs(guildId, props.id)
  }
}

onMounted(loadLogs)

watch(
  () => [props.kind, props.id],
  async () => {
    logListRef.value?.reset()
    await loadLogs()
  },
)
</script>

<style scoped>
.log-pane {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  background: #080500;
  font-family: var(--font-mono);
  overflow: hidden;
}

.kind-agent :deep(.pane-title) {
  color: var(--color-amber);
}
.kind-worker :deep(.pane-title) {
  color: var(--color-teal);
}
.kind-task :deep(.pane-title) {
  color: var(--color-text);
}

.kind-worker :deep(.title-icon) {
  color: var(--color-teal);
}
.kind-task :deep(.title-icon) {
  color: var(--color-brass);
}

.kind-agent :deep(.live-indicator.active) {
  color: var(--color-green);
  animation: livePulse 1.5s infinite;
}
.kind-worker :deep(.live-indicator.active) {
  color: var(--color-teal);
  animation: livePulse 1.5s infinite;
}

@keyframes livePulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.4;
  }
}

/* Task header badges */
.phase-badge {
  font-family: var(--font-pixel);
  font-size: 6px;
  padding: 2px 5px;
  border: 1px solid;
  text-transform: uppercase;
  letter-spacing: 1px;
  flex-shrink: 0;
}
.phase-badge.plan {
  color: var(--color-blue);
  border-color: var(--color-blue);
}
.phase-badge.execute {
  color: var(--color-teal);
  border-color: var(--color-teal);
}
.phase-badge.review {
  color: var(--color-amber);
  border-color: var(--color-amber);
}
.phase-badge.followup {
  color: var(--color-orange);
  border-color: var(--color-orange);
}

.state-badge {
  font-size: 6px;
  padding: 2px 6px;
  background: none;
  border: 1px solid;
  font-family: var(--font-pixel);
  letter-spacing: 1px;
  text-transform: uppercase;
}
.state-pending {
  color: var(--color-text-dim);
  border-color: var(--color-text-dim);
}
.state-planning {
  color: var(--color-blue);
  border-color: var(--color-blue);
}
.state-working {
  color: var(--color-green);
  border-color: var(--color-green);
  animation: statePulse 1s infinite;
}
.state-awaiting-review {
  color: var(--color-amber);
  border-color: var(--color-amber);
}
.state-done {
  color: var(--color-teal);
  border-color: var(--color-teal);
}
.state-failed {
  color: var(--color-red);
  border-color: var(--color-red);
}
.state-cancelled {
  color: var(--color-red);
  border-color: var(--color-red);
  opacity: 0.7;
}
.state-follow-up {
  color: var(--color-orange);
  border-color: var(--color-orange);
}

@keyframes statePulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.4;
  }
}
</style>
