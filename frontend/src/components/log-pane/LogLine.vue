<template>
  <div class="log-entry">
    <div
      class="log-line"
      :class="{ 'log-line--expandable': isExpandable }"
      @click="isExpandable && $emit('toggle')"
    >
      <span class="log-time">{{ formatTime(log.timestamp) }}</span>
      <span
        class="log-text"
        :class="[
          lineClass,
          { 'log-text--markdown': isMarkdownLine, 'markdown-prose': isMarkdownLine },
        ]"
        v-html="renderedLine"
        v-artifacts
      ></span>
      <span v-if="isExpandable" class="log-expand-icon">{{ expanded ? '▲' : '▼' }}</span>
    </div>
    <div v-if="isExpandable && expanded" class="log-detail">
      <template v-if="log.detail.toolType === 'tool_use'">
        <template v-if="toolName === 'edit'">
          <div class="log-detail-label">OLD</div>
          <pre class="log-detail-body log-detail-old">{{
            inputRecord(log.detail.input).old_string ?? inputRecord(log.detail.input).oldText
          }}</pre>
          <div class="log-detail-label log-detail-label--new">NEW</div>
          <pre class="log-detail-body log-detail-new">{{
            inputRecord(log.detail.input).new_string ?? inputRecord(log.detail.input).newText
          }}</pre>
        </template>
        <template v-else-if="toolName === 'write'">
          <div class="log-detail-label">
            {{ inputRecord(log.detail.input).file_path ?? inputRecord(log.detail.input).path }}
          </div>
          <pre class="log-detail-body">{{ inputRecord(log.detail.input).content }}</pre>
        </template>
        <template v-else-if="toolName === 'bash'">
          <div class="log-detail-label">COMMAND</div>
          <pre class="log-detail-body">{{ inputRecord(log.detail.input).command }}</pre>
        </template>
        <template v-else-if="toolName === 'read'">
          <div class="log-detail-label">FILE</div>
          <pre class="log-detail-body">{{
            inputRecord(log.detail.input).path ?? inputRecord(log.detail.input).file_path
          }}</pre>
          <div class="log-detail-label">OPTIONS</div>
          <pre class="log-detail-body">{{ JSON.stringify(log.detail.input, null, 2) }}</pre>
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
      <template v-else-if="log.detail.toolType === 'thinking'">
        <div class="log-detail-label">FULL THOUGHT</div>
        <pre class="log-detail-body log-detail-thinking">{{ log.detail.fullText }}</pre>
      </template>
      <!-- Fallback for unknown/unrecognized toolTypes: show full JSON payload -->
      <template v-else>
        <div class="log-detail-label">RAW JSON</div>
        <pre class="log-detail-body log-detail-raw">{{ formatDetailJson(log) }}</pre>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { renderMarkdown } from '../../utils/markdown'
import { vArtifacts } from '../../utils/artifacts'
import { formatClock } from '../../utils/format'
import { claudeEventSummary } from '../../utils/claudeEventSummary'
import { piEventSummary } from '../../utils/piEventSummary'
import type { LogEntry } from '../../types'


const props = defineProps<{ log: LogEntry; expanded: boolean }>()

defineEmits<{ toggle: [] }>()

const toolName = computed(() => props.log.detail?.name?.toLowerCase())

const isExpandable = computed(() => {
  const log = props.log
  if (!log.detail?.toolType) return false
  if (log.detail.toolType === 'thinking') return log.detail.input !== log.detail.summary
  if (log.detail.toolType === 'tool_result') {
    // Pi truncates even short results and long single lines; compare against the preview.
    const output = log.detail.output?.trim()
    return Boolean(output && !log.line.includes(output))
  }
  // Known tool types are expandable (tool_use rules above). Unknown types also expand
  // to show their raw JSON payload.
  return true
})

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

const eventSummary = computed(() => {
  if (props.log.detail?.toolType === 'tool_result' && props.log.detail.is_error === true)
    return `✗ Tool failed · ${props.log.line}`
  return claudeEventSummary(props.log.detail) ?? piEventSummary(props.log.detail)
})

const isMarkdownLine = computed(() => {
  if (eventSummary.value !== undefined) return false
  const log = props.log
  const line = log.line
  if (!line) return false
  // Tool glyphs are intrinsic plain-text formatting from Claude's own output.
  if (
    line.startsWith('▶') ||
    line.startsWith('✓') ||
    line.startsWith('✗') ||
    line.startsWith('  →')
  )
    return false
  // Typed lines: worker/auth status is plain; claude/thinking framing is markdown.
  if (log.level === 'worker' || log.level === 'auth') return false
  if (log.level === 'claude' || log.level === 'thinking') return true
  // Legacy fallback for pre-typed persisted logs (bracketed prefixes).
  if (line.startsWith('[') && !line.startsWith('[thinking]') && !line.startsWith('[claude]'))
    return false
  return true
})

const renderedLine = computed(() => {
  if (isMarkdownLine.value) return renderMarkdown(props.log.line)
  return linkify(eventSummary.value ?? props.log.line)
})

function inputRecord(input: Record<string, unknown> | string | undefined): Record<string, unknown> {
  return typeof input === 'object' && input !== null ? input : {}
}

function formatDetailJson(log: LogEntry): string {
  // For unknown/unsupported toolTypes, show the full detail + line info
  const detail = log.detail
  if (!detail) {
    return JSON.stringify({ line: log.line, timestamp: log.timestamp }, null, 2)
  }
  // Runner event entries keep their original payload under event.
  if ((detail.toolType === 'claude_json' || detail.toolType === 'pi_json') && detail.event) {
    return JSON.stringify(detail.event, null, 2)
  }
  // Otherwise show full detail object
  const { ...rest } = detail
  return JSON.stringify(rest, null, 2)
}

const lineClass = computed(() => {
  const log = props.log
  const line = log.line
  if (!line) return ''
  if (log.detail?.is_error === true) return 'log-error'
  // Tool glyphs carry intrinsic success/error/tool semantics from Claude's
  // own output formatting, so they win regardless of level.
  if (line.startsWith('✓') || line.includes('Done')) return 'log-success'
  if (line.startsWith('✗') || line.includes('error') || line.includes('Error')) return 'log-error'
  if (line.startsWith('▶')) return 'log-tool'
  if (line.startsWith('  →')) return 'log-result'
  // Typed lines are styled by level instead of sniffing text prefixes.
  if (log.level === 'thinking') return 'log-thinking'
  if (log.level === 'worker' || log.level === 'auth' || log.level === 'claude') return 'log-meta'
  // Legacy fallback for pre-typed persisted logs (bracketed prefixes).
  if (line.startsWith('[thinking]')) return 'log-thinking'
  if (line.startsWith('[')) return 'log-meta'
  return ''
})
</script>

<style scoped>
.log-entry {
  display: flex;
  flex-direction: column;
}

.log-line {
  display: flex;
  gap: 12px;
  font: var(--text-small) / var(--leading-body) var(--font-mono);
  min-width: 0;
}

.log-line--expandable {
  cursor: pointer;
  border-radius: 2px;
}
.log-line--expandable:hover {
  background: rgba(255, 255, 255, 0.04);
}

.log-expand-icon {
  font-size: var(--text-caption);
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
  font: 600 var(--text-caption) / 1.4 var(--font-ui);
  color: var(--color-text-dim);
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
  font: var(--text-small) / var(--leading-body) var(--font-mono);
  color: var(--color-text-dim);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
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
.log-detail-thinking {
  background: rgba(30, 60, 120, 0.1);
  border-color: rgba(80, 140, 220, 0.25);
  color: var(--color-blue);
  font-style: italic;
}

.log-detail-raw {
  background: rgba(120, 60, 120, 0.1);
  border-color: rgba(160, 100, 160, 0.25);
  color: #d0a0d0;
}

.log-time {
  color: var(--color-text-dim);
  flex-shrink: 0;
  font-size: var(--text-caption);
  font-variant-numeric: tabular-nums;
}

.log-text {
  color: var(--color-green);
  min-width: 0;
  overflow-wrap: anywhere;
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
.log-text.log-meta:not(.log-text--markdown) {
  color: var(--color-text-dim);
}
.log-text.log-thinking {
  color: var(--color-blue);
  font-style: italic;
}

.log-text--markdown {
  color: var(--color-text);
  white-space: normal;
}

.log-text :deep(.terminal-link) {
  color: var(--color-blue);
  text-decoration: underline;
  cursor: pointer;
}
@media (max-width: 700px) {
  .log-line {
    gap: 8px;
  }

  .log-detail {
    margin-left: 0;
  }
}
</style>
