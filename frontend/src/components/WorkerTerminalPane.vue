<template>
  <div class="worker-terminal">
    <div class="terminal-header">
      <div class="terminal-title">
        <span class="term-icon">⚙</span>
        <span>{{ worker ? worker.name : workerId }}</span>
      </div>
      <div class="terminal-meta">
        <span v-if="worker" class="state-badge" :class="worker.state">{{ worker.state }}</span>
        <span class="live-indicator" :class="{ active: worker && !['idle', 'offline'].includes(worker.state) }">
          {{ worker && !['idle', 'offline'].includes(worker.state) ? '● LIVE' : '○ IDLE' }}
        </span>
      </div>
    </div>

    <div class="terminal-body" ref="terminalEl">
      <div class="terminal-welcome">
        <pre>╔══════════════════════════════════════╗
║    PIONEER SQUARE TERMINAL v1.0      ║
║    Worker: {{ padName(worker?.name) }}   ║
╚══════════════════════════════════════╝</pre>
      </div>
      <div v-if="logs.length === 0" class="terminal-empty">
        <span class="cursor-blink">_</span> Waiting for output...
      </div>
      <div v-for="(log, i) in logs" :key="i" class="terminal-entry">
        <div
          class="terminal-line"
          :class="{ 'terminal-line--expandable': !!log.detail }"
          @click="log.detail && toggleDetail(i)"
        >
          <span class="log-time">{{ formatTime(log.timestamp) }}</span>
          <span class="log-content" :class="lineClass(log.line)">{{ log.line }}</span>
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
      <div class="terminal-prompt">
        <span class="prompt-user">worker@pioneer-square</span>
        <span class="prompt-sep">:</span>
        <span class="prompt-path">~/workspace</span>
        <span class="prompt-dollar">$</span>
        <span class="cursor-blink">_</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted } from 'vue'
import { useAgentsStore } from '../stores/agents'
import { useGuildStore } from '../stores/guild'

const props = defineProps<{
  workerId: string
}>()

const agentsStore = useAgentsStore()
const guildStore = useGuildStore()
const terminalEl = ref<HTMLElement | null>(null)
const expandedIdx = ref<number | null>(null)

const worker = computed(() => agentsStore.workers.find(w => w.id === props.workerId))
const logs = computed(() => agentsStore.workerLogs[props.workerId] || [])

function toggleDetail(i: number) {
  expandedIdx.value = expandedIdx.value === i ? null : i
}

onMounted(async () => {
  const guildId = guildStore.currentGuild?.id
  if (guildId) await agentsStore.fetchWorkerLogs(guildId, props.workerId)
})

function padName(name?: string) {
  if (!name) return ''.padEnd(20)
  return name.slice(0, 20).padEnd(20)
}

function formatTime(isoStr?: string) {
  if (!isoStr) return '00:00:00'
  return new Date(isoStr).toLocaleTimeString('en-US', { hour12: false })
}

function lineClass(line: string) {
  if (!line) return ''
  if (line.startsWith('✓')) return 'log-success'
  if (line.startsWith('✗')) return 'log-error'
  if (line.startsWith('[worker]')) return 'log-meta'
  return ''
}

watch(logs, async () => {
  await nextTick()
  if (terminalEl.value) terminalEl.value.scrollTop = terminalEl.value.scrollHeight
}, { deep: true })
</script>

<style scoped>
.worker-terminal {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  background: #080500;
  font-family: var(--font-mono);
}

.terminal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 16px;
  background: #0f0a02;
  border-bottom: 2px solid #2a1a05;
  flex-shrink: 0;
}

.terminal-title {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--color-teal);
  font-size: 13px;
}

.term-icon { color: var(--color-teal); }

.terminal-meta {
  display: flex;
  align-items: center;
  gap: 12px;
}

.state-badge {
  font-size: 10px;
  padding: 2px 8px;
  border-radius: 2px;
  text-transform: uppercase;
  font-family: var(--font-pixel);
  letter-spacing: 1px;
}
.state-badge.idle    { background: rgba(154,128,96,0.2); color: var(--color-text-dim); }
.state-badge.working { background: rgba(0,255,136,0.2);  color: var(--color-green); }
.state-badge.busy    { background: rgba(255,136,68,0.2); color: var(--color-orange); }
.state-badge.error   { background: rgba(255,51,51,0.2);  color: var(--color-red); }
.state-badge.offline { background: rgba(100,100,100,0.2); color: var(--color-text-dim); }

.live-indicator {
  font-size: 11px;
  color: var(--color-text-dim);
}
.live-indicator.active {
  color: var(--color-teal);
  animation: livePulse 1.5s infinite;
}
@keyframes livePulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }

.terminal-body {
  flex: 1;
  overflow-y: auto;
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.terminal-welcome pre {
  color: var(--color-brass-dark);
  font-size: 11px;
  margin-bottom: 12px;
  line-height: 1.5;
}

.terminal-empty {
  color: var(--color-text-dim);
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: 4px;
}

.terminal-entry { display: flex; flex-direction: column; }

.terminal-line {
  display: flex;
  gap: 12px;
  font-size: 13px;
  line-height: 1.6;
}

.terminal-line--expandable {
  cursor: pointer;
  border-radius: 2px;
}
.terminal-line--expandable:hover { background: rgba(255,255,255,0.04); }

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
.log-detail-label:first-child { margin-top: 0; }
.log-detail-label--new { color: var(--color-green); }

.log-detail-body {
  margin: 0 0 2px;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--color-text-dim);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 300px;
  overflow-y: auto;
  background: rgba(0,0,0,0.3);
  border: 1px solid #2a1a05;
  padding: 6px 8px;
}
.log-detail-old {
  background: rgba(180,30,30,0.08);
  border-color: rgba(220,50,50,0.3);
  color: #c07070;
}
.log-detail-new {
  background: rgba(0,120,60,0.08);
  border-color: rgba(0,187,100,0.3);
  color: #70c090;
}

.log-time {
  color: var(--color-text-dim);
  flex-shrink: 0;
  font-size: 11px;
}

.log-content         { color: var(--color-green); word-break: break-all; white-space: pre-wrap; }
.log-content.log-success { color: var(--color-teal); }
.log-content.log-error   { color: var(--color-red); }
.log-content.log-meta    { color: var(--color-brass-dark); }

.terminal-prompt {
  margin-top: 8px;
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: 2px;
}
.prompt-user   { color: var(--color-teal); }
.prompt-sep    { color: var(--color-text-dim); }
.prompt-path   { color: var(--color-blue); }
.prompt-dollar { color: var(--color-text); margin: 0 4px; }

.cursor-blink {
  color: var(--color-teal);
  animation: blink 1s step-end infinite;
}
@keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }
</style>
