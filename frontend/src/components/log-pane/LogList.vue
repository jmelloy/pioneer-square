<template>
  <div class="pane-body" ref="bodyEl">
    <div v-if="logs.length === 0" class="logs-empty">
      <span class="cursor-blink">_</span> Waiting for output…
    </div>
    <div v-for="(log, i) in logs" :key="i" class="log-entry">
      <div
        class="log-line"
        :class="{ 'log-line--expandable': !!log.detail }"
        @click="log.detail && toggleDetail(i)"
      >
        <span class="log-time">{{ formatTime(log.timestamp) }}</span>
        <span
          class="log-text"
          :class="[lineClass(log.line), { 'log-text--markdown': isMarkdownLine(log.line) }]"
          v-html="renderLine(log.line)"
        ></span>
        <span v-if="log.detail" class="log-expand-icon">{{ expandedIdx === i ? '▲' : '▼' }}</span>
      </div>
      <div v-if="log.detail && expandedIdx === i" class="log-detail">
        <template v-if="log.detail.toolType === 'tool_use'">
          <template v-if="log.detail.name === 'Edit'">
            <div class="log-detail-label">OLD</div>
            <pre class="log-detail-body log-detail-old">{{ log.detail.input?.old_string }}</pre>
            <div class="log-detail-label log-detail-label--new">NEW</div>
            <pre class="log-detail-body log-detail-new">{{ log.detail.input?.new_string }}</pre>
          </template>
          <template v-else-if="log.detail.name === 'Write'">
            <div class="log-detail-label">{{ log.detail.input?.file_path }}</div>
            <pre class="log-detail-body">{{ log.detail.input?.content }}</pre>
          </template>
          <template v-else-if="log.detail.name === 'Bash'">
            <div class="log-detail-label">COMMAND</div>
            <pre class="log-detail-body">{{ log.detail.input?.command }}</pre>
          </template>
          <template v-else>
            <div class="log-detail-label">{{ log.detail.name }}</div>
            <pre class="log-detail-body">{{ JSON.stringify(log.detail.input, null, 2) }}</pre>
          </template>
        </template>
        <template v-else-if="log.detail.toolType === 'tool_result'">
          <div class="log-detail-label">OUTPUT</div>
          <pre class="log-detail-body">{{ log.detail.output }}</pre>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, nextTick } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { formatClock } from '../../utils/format'
import type { LogEntry } from '../../types'

const props = defineProps<{ logs: LogEntry[] }>()

const bodyEl = ref<HTMLElement | null>(null)
const expandedIdx = ref<number | null>(null)

function toggleDetail(i: number) {
  expandedIdx.value = expandedIdx.value === i ? null : i
}

const formatTime = (iso?: string) => formatClock(iso, true)

function linkify(text: string): string {
  const escaped = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
  return escaped.replace(
    /(https?:\/\/[^\s<>"]+)/g,
    '<a href="$1" target="_blank" rel="noopener noreferrer" class="terminal-link">$1</a>',
  )
}

function isMarkdownLine(line: string): boolean {
  return line.startsWith('[thinking]') || line.startsWith('[claude]')
}

function renderMarkdown(text: string): string {
  const html = marked.parse(text, { async: false }) as string
  return DOMPurify.sanitize(html, { ADD_ATTR: ['target', 'rel'] })
}

function renderLine(line: string): string {
  if (isMarkdownLine(line)) return renderMarkdown(line)
  return linkify(line)
}

function lineClass(line: string) {
  if (!line) return ''
  if (line.startsWith('✓') || line.includes('Done')) return 'log-success'
  if (line.startsWith('✗') || line.includes('error') || line.includes('Error')) return 'log-error'
  if (line.startsWith('▶')) return 'log-tool'
  if (line.startsWith('  →')) return 'log-result'
  if (line.startsWith('[worker]')) return 'log-meta'
  if (line.startsWith('[thinking]')) return 'log-thinking'
  if (line.startsWith('[')) return 'log-meta'
  return ''
}

defineExpose({
  bodyEl,
  reset: () => {
    expandedIdx.value = null
  },
})

watch(
  () => props.logs,
  async () => {
    await nextTick()
    if (bodyEl.value) bodyEl.value.scrollTop = bodyEl.value.scrollHeight
  },
  { deep: true },
)
</script>

<style scoped>
.pane-body {
  flex: 1;
  overflow-y: auto;
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.logs-empty {
  color: var(--color-text-dim);
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: 4px;
  font-style: italic;
  padding: 8px 0;
}

.log-entry {
  display: flex;
  flex-direction: column;
}

.log-line {
  display: flex;
  gap: 12px;
  font-size: 13px;
  line-height: 1.6;
}

.log-line--expandable {
  cursor: pointer;
  border-radius: 2px;
}
.log-line--expandable:hover {
  background: rgba(255, 255, 255, 0.04);
}

.log-expand-icon {
  font-size: 8px;
  color: var(--color-text-dim);
  flex-shrink: 0;
  margin-left: auto;
  opacity: 0.5;
}

.log-detail {
  margin: 3px 0 4px 56px;
  border-left: 2px solid #2a1a05;
  padding-left: 10px;
}

.log-detail-label {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: #5a3a10;
  letter-spacing: 1px;
  margin-bottom: 4px;
  margin-top: 6px;
}
.log-detail-label:first-child {
  margin-top: 0;
}
.log-detail-label--new {
  color: var(--color-green);
}

.log-detail-body {
  margin: 0 0 2px;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--color-text-dim);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 300px;
  overflow-y: auto;
  background: rgba(0, 0, 0, 0.3);
  border: 1px solid #2a1a05;
  padding: 6px 8px;
}
.log-detail-old {
  background: rgba(180, 30, 30, 0.08);
  border-color: rgba(220, 50, 50, 0.3);
  color: #c07070;
}
.log-detail-new {
  background: rgba(0, 120, 60, 0.08);
  border-color: rgba(0, 187, 100, 0.3);
  color: #70c090;
}

.log-time {
  color: var(--color-text-dim);
  flex-shrink: 0;
  font-size: 11px;
}

.log-text {
  color: var(--color-green);
  word-break: break-all;
  white-space: pre-wrap;
}
.log-text.log-success {
  color: var(--color-teal);
}
.log-text.log-error {
  color: var(--color-red);
}
.log-text.log-tool {
  color: var(--color-amber);
}
.log-text.log-result {
  color: var(--color-text-dim);
}
.log-text.log-meta {
  color: var(--color-brass-dark);
}
.log-text.log-thinking {
  color: var(--color-blue);
  font-style: italic;
}

.log-text--markdown {
  white-space: normal;
}
.log-text--markdown :deep(p) {
  margin: 0 0 4px;
}
.log-text--markdown :deep(p:last-child) {
  margin-bottom: 0;
}
.log-text--markdown :deep(ul),
.log-text--markdown :deep(ol) {
  margin: 4px 0 4px;
  padding-left: 18px;
}
.log-text--markdown :deep(li) {
  margin-bottom: 2px;
}
.log-text--markdown :deep(code) {
  font-family: var(--font-mono);
  font-size: 11px;
  background: rgba(0, 0, 0, 0.35);
  border: 1px solid var(--color-brass-dark);
  padding: 0 4px;
  border-radius: 2px;
  color: var(--color-amber);
}
.log-text--markdown :deep(pre) {
  background: rgba(0, 0, 0, 0.4);
  border: 1px solid var(--color-brass-dark);
  padding: 8px 10px;
  overflow-x: auto;
  margin: 4px 0;
}
.log-text--markdown :deep(pre code) {
  background: none;
  border: none;
  padding: 0;
  font-size: 10px;
  color: var(--color-green);
}
.log-text--markdown :deep(h1),
.log-text--markdown :deep(h2),
.log-text--markdown :deep(h3) {
  font-family: var(--font-pixel);
  font-size: 8px;
  letter-spacing: 1px;
  color: var(--color-teal);
  margin: 6px 0 4px;
  text-transform: uppercase;
}
.log-text--markdown :deep(strong) {
  color: var(--color-amber);
  font-weight: bold;
}
.log-text--markdown :deep(em) {
  color: var(--color-text-dim);
  font-style: italic;
}
.log-text--markdown :deep(a) {
  color: var(--color-teal);
  text-decoration: underline;
}
.log-text--markdown :deep(blockquote) {
  border-left: 3px solid var(--color-brass-dark);
  margin: 4px 0;
  padding: 2px 8px;
  color: var(--color-text-dim);
  font-style: italic;
}

.terminal-link {
  color: var(--color-blue);
  text-decoration: underline;
  cursor: pointer;
}

.cursor-blink {
  color: var(--color-amber);
  animation: blink 1s step-end infinite;
}

@keyframes blink {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0;
  }
}
</style>
