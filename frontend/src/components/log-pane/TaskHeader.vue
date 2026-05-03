<template>
  <div v-if="taskBranch || taskPrUrl || taskCreatedAt" class="pane-subheader">
    <span v-if="taskId" class="sub-id">{{ taskId }}</span>
    <span v-if="taskBranch" class="sub-branch">⌥ {{ taskBranch }}</span>
    <a v-if="taskPrUrl" :href="taskPrUrl" target="_blank" rel="noopener" class="sub-pr-link"
      >PR →</a
    >
    <span v-if="taskCreatedAt" class="sub-time">{{ formatDateTime(taskCreatedAt) }}</span>
  </div>

  <div v-if="taskDescription" class="pane-description">
    <div class="pane-description-label">TASK</div>
    <div
      class="pane-description-body task-description--markdown"
      v-html="renderedDescription"
    ></div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

const props = defineProps<{
  taskId?: string
  taskBranch?: string
  taskPrUrl?: string | null
  taskCreatedAt?: string
  taskDescription?: string
}>()

const renderedDescription = computed(() => {
  if (!props.taskDescription) return ''
  const html = marked.parse(props.taskDescription, { async: false }) as string
  return DOMPurify.sanitize(html, { ADD_ATTR: ['target', 'rel'] })
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
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-text-dim);
}

.sub-branch {
  font-size: 10px;
  color: var(--color-teal);
  font-family: var(--font-mono);
  word-break: break-all;
}

.sub-pr-link {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-teal);
  text-decoration: none;
  padding: 2px 6px;
  border: 1px solid var(--color-teal);
}
.sub-pr-link:hover {
  background: rgba(0, 187, 170, 0.15);
}

.sub-time {
  font-size: 10px;
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
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 2px;
  color: var(--color-brass-dark);
  margin-bottom: 6px;
}

.pane-description-body {
  font-size: 12px;
  color: var(--color-text);
  line-height: 1.55;
}

.task-description--markdown :deep(p) {
  margin: 0 0 6px;
}
.task-description--markdown :deep(p:last-child) {
  margin-bottom: 0;
}
.task-description--markdown :deep(ul),
.task-description--markdown :deep(ol) {
  margin: 4px 0 6px;
  padding-left: 18px;
}
.task-description--markdown :deep(li) {
  margin: 2px 0;
}
.task-description--markdown :deep(code) {
  font-family: var(--font-mono);
  font-size: 11px;
  background: rgba(255, 255, 255, 0.07);
  padding: 1px 4px;
  border-radius: 2px;
  color: var(--color-teal);
}
.task-description--markdown :deep(pre) {
  background: rgba(0, 0, 0, 0.35);
  border: 1px solid #2a1a05;
  padding: 6px 10px;
  overflow-x: auto;
  margin: 6px 0;
}
.task-description--markdown :deep(pre code) {
  background: none;
  padding: 0;
  font-size: 11px;
  color: var(--color-green);
}
.task-description--markdown :deep(strong) {
  color: var(--color-amber);
  font-weight: 700;
}
.task-description--markdown :deep(em) {
  color: var(--color-text-dim);
  font-style: italic;
}
.task-description--markdown :deep(a) {
  color: var(--color-teal);
  text-decoration: underline;
}
.task-description--markdown :deep(h1),
.task-description--markdown :deep(h2),
.task-description--markdown :deep(h3) {
  font-family: var(--font-pixel);
  color: var(--color-brass-light);
  margin: 8px 0 4px;
}
.task-description--markdown :deep(blockquote) {
  border-left: 3px solid var(--color-brass-dark);
  margin: 4px 0;
  padding-left: 10px;
  color: var(--color-text-dim);
}
</style>
