<template>
  <div v-if="taskBranch || taskPrUrl || taskCreatedAt" class="pane-subheader">
    <span v-if="taskId" class="sub-id">{{ taskId }}</span>
    <span v-if="taskBranch" class="sub-branch">⌥ {{ taskBranch }}</span>
    <a
      v-if="taskPrUrl"
      :href="taskPrUrl"
      target="_blank"
      rel="noopener noreferrer"
      class="sub-pr-link"
      >PR →</a
    >
    <span v-if="taskCreatedAt" class="sub-time">{{ formatDateTime(taskCreatedAt) }}</span>
  </div>

  <div v-if="taskDescription" class="pane-description">
    <div class="pane-description-label">TASK</div>
    <div
      class="pane-description-body task-description--markdown markdown-prose"
      v-html="renderedDescription"
      v-artifacts="guildStore.currentGuild?.id"
    ></div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { renderMarkdown } from '../../utils/markdown'
import { vArtifacts } from '../../utils/artifacts'
import { useGuildStore } from '../../stores/guild'

const guildStore = useGuildStore()

const props = defineProps<{
  taskId?: string
  taskBranch?: string
  taskPrUrl?: string | null
  taskCreatedAt?: string
  taskDescription?: string
}>()

const renderedDescription = computed(() => {
  if (!props.taskDescription) return ''
  return renderMarkdown(props.taskDescription)
})

function formatDateTime(iso?: string) {
  if (!iso) return ''
  return new Date(iso).toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}
</script>

<style scoped>
.pane-subheader {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 6px 16px;
  background: var(--color-bg-secondary);
  border-bottom: 1px solid var(--color-brass-dark);
  flex-shrink: 0;
}

.sub-id {
  font: var(--text-caption) / 1.4 var(--font-mono);
  overflow-wrap: anywhere;
  color: var(--color-text-dim);
}

.sub-branch {
  font-size: var(--text-caption);
  color: var(--color-teal);
  font-family: var(--font-mono);
  word-break: break-all;
}

.sub-pr-link {
  font: 600 var(--text-small) / 1.4 var(--font-ui);
  color: var(--color-teal);
  text-decoration: none;
  padding: 2px 6px;
  border: 1px solid var(--color-teal);
}
.sub-pr-link:hover {
  background: rgba(0, 187, 170, 0.15);
}

.sub-time {
  font-size: var(--text-caption);
  font-variant-numeric: tabular-nums;
  color: var(--color-text-dim);
  margin-left: auto;
}

.pane-description {
  flex-shrink: 0;
  max-height: 180px;
  overflow-y: auto;
  padding: 8px 16px 10px;
  border-bottom: 1px solid #2a1a05;
  background: rgba(255, 255, 255, 0.015);
}

.pane-description-label {
  font: 600 var(--text-caption) / 1.4 var(--font-ui);
  color: var(--color-text-dim);
  margin-bottom: 6px;
}

.pane-description-body {
  color: var(--color-text);
}
</style>
