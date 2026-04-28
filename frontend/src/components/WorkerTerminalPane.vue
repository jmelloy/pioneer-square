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
      <div v-for="(log, i) in logs" :key="i" class="terminal-line">
        <span class="log-time">{{ formatTime(log.timestamp) }}</span>
        <span class="log-content" :class="lineClass(log.line)">{{ log.line }}</span>
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

<script setup>
import { ref, computed, watch, nextTick, onMounted } from 'vue'
import { useAgentsStore } from '../stores/agents.js'
import { useGuildStore } from '../stores/guild.js'

const props = defineProps({
  workerId: { type: String, required: true }
})

const agentsStore = useAgentsStore()
const guildStore = useGuildStore()
const terminalEl = ref(null)

const worker = computed(() => agentsStore.workers.find(w => w.id === props.workerId))
const logs = computed(() => agentsStore.workerLogs[props.workerId] || [])

onMounted(async () => {
  const guildId = guildStore.currentGuild?.id
  if (guildId) await agentsStore.fetchWorkerLogs(guildId, props.workerId)
})

function padName(name) {
  if (!name) return ''.padEnd(20)
  return name.slice(0, 20).padEnd(20)
}

function formatTime(isoStr) {
  if (!isoStr) return '00:00:00'
  return new Date(isoStr).toLocaleTimeString('en-US', { hour12: false })
}

function lineClass(line) {
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

.terminal-line {
  display: flex;
  gap: 12px;
  font-size: 13px;
  line-height: 1.6;
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
