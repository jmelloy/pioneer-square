<template>
  <div class="terminal-pane">
    <div class="terminal-header">
      <div class="terminal-title">
        <span class="term-icon">▶</span>
        <span>{{ agent ? agent.name : 'Unknown Agent' }}</span>
      </div>
      <div class="terminal-meta">
        <span v-if="agent" class="agent-state-badge" :class="agent.state">
          {{ agent.state }}
        </span>
        <span class="live-indicator" :class="{ active: agent && agent.state !== 'idle' }">
          {{ agent && agent.state !== 'idle' ? '● LIVE' : '○ IDLE' }}
        </span>
      </div>
    </div>

    <div class="terminal-body" ref="terminalEl">
      <div class="terminal-welcome">
        <pre>╔══════════════════════════════════════╗
║    PIONEER SQUARE TERMINAL v1.0      ║
║    Agent: {{ padName(agent?.name) }}      ║
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
        <span class="prompt-user">agent@pioneer-square</span>
        <span class="prompt-sep">:</span>
        <span class="prompt-path">~/workspace</span>
        <span class="prompt-dollar">$</span>
        <span class="cursor-blink">_</span>
      </div>
    </div>

    <!-- Run command bar -->
    <div class="run-bar">
      <select v-model="runTool" class="tool-select" :disabled="isRunning">
        <option value="claude">claude</option>
        <option value="codex">codex</option>
        <option value="pi">pi</option>
      </select>
      <input
        v-model="runModel"
        class="model-input"
        :placeholder="modelPlaceholder"
        :disabled="isRunning"
        title="Model (optional)"
      />
      <input
        v-if="runTool === 'pi'"
        v-model="runProvider"
        class="provider-input"
        placeholder="provider"
        :disabled="isRunning"
        title="Provider (anthropic, openai, google…)"
      />
      <input
        v-model="runPrompt"
        class="prompt-input"
        placeholder="Enter task prompt…"
        :disabled="isRunning"
        @keydown.enter.exact="handleRun"
      />
      <button
        v-if="!isRunning"
        class="run-btn pixel-btn"
        :disabled="!runPrompt.trim()"
        @click="handleRun"
        title="Run agent"
      >▶ RUN</button>
      <button
        v-else
        class="stop-btn pixel-btn"
        @click="handleStop"
        title="Stop agent"
      >■ STOP</button>
    </div>

    <div v-if="runError" class="run-error">{{ runError }}</div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted } from 'vue'
import { useAgentsStore } from '../stores/agents'
import { useGuildStore } from '../stores/guild'

const props = defineProps<{
  agentId: string
}>()

const agentsStore = useAgentsStore()
const guildStore = useGuildStore()
const terminalEl = ref<HTMLElement | null>(null)
const expandedIdx = ref<number | null>(null)

const agent = computed(() => agentsStore.agents.find(a => a.id === props.agentId))

function toggleDetail(i: number) {
  expandedIdx.value = expandedIdx.value === i ? null : i
}

onMounted(async () => {
  const guildId = guildStore.currentGuild?.id
  if (guildId) await agentsStore.fetchAgentLogs(guildId, props.agentId)
})
const logs = computed(() => agent.value?.logs || [])
const isRunning = computed(() => agent.value?.state === 'working' || agent.value?.state === 'thinking' || agent.value?.state === 'busy')

const runTool = ref('claude')
const runPrompt = ref('')
const runModel = ref('')
const runProvider = ref('')
const runError = ref('')

const modelPlaceholder = computed(() => {
  if (runTool.value === 'claude') return 'claude-opus-4-7'
  if (runTool.value === 'codex') return 'o4-mini'
  return 'model (optional)'
})

function padName(name?: string) {
  if (!name) return ''.padEnd(20)
  return name.slice(0, 20).padEnd(20)
}

function formatTime(isoStr?: string) {
  if (!isoStr) return '00:00:00'
  const d = new Date(isoStr)
  return d.toLocaleTimeString('en-US', { hour12: false })
}

function lineClass(line: string) {
  if (!line) return ''
  if (line.startsWith('✓')) return 'log-success'
  if (line.startsWith('✗')) return 'log-error'
  if (line.startsWith('▶')) return 'log-tool'
  if (line.startsWith('  →')) return 'log-result'
  if (line.startsWith('[')) return 'log-meta'
  return ''
}

async function handleRun() {
  if (!runPrompt.value.trim() || isRunning.value) return
  runError.value = ''
  try {
    await agentsStore.runAgent(props.agentId, {
      tool: runTool.value,
      prompt: runPrompt.value.trim(),
      model: runModel.value.trim(),
      provider: runProvider.value.trim()
    })
    runPrompt.value = ''
  } catch (e: any) {
    runError.value = e.message
  }
}

async function handleStop() {
  runError.value = ''
  try {
    await agentsStore.stopAgent(props.agentId)
  } catch (e: any) {
    runError.value = e.message
  }
}

watch(logs, async () => {
  await nextTick()
  if (terminalEl.value) {
    terminalEl.value.scrollTop = terminalEl.value.scrollHeight
  }
}, { deep: true })
</script>

<style scoped>
.terminal-pane {
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
  color: var(--color-amber);
  font-size: 13px;
}

.term-icon {
  color: var(--color-green);
}

.terminal-meta {
  display: flex;
  align-items: center;
  gap: 12px;
}

.agent-state-badge {
  font-size: 10px;
  padding: 2px 8px;
  border-radius: 2px;
  text-transform: uppercase;
  font-family: var(--font-pixel);
  letter-spacing: 1px;
}

.agent-state-badge.idle    { background: rgba(154,128,96,0.2); color: var(--color-text-dim); }
.agent-state-badge.thinking{ background: rgba(68,153,255,0.2); color: var(--color-blue); }
.agent-state-badge.working { background: rgba(0,255,136,0.2);  color: var(--color-green); }
.agent-state-badge.busy    { background: rgba(255,136,68,0.2); color: var(--color-orange); }
.agent-state-badge.error   { background: rgba(255,51,51,0.2);  color: var(--color-red); }

.live-indicator {
  font-size: 11px;
  color: var(--color-text-dim);
}
.live-indicator.active {
  color: var(--color-green);
  animation: livePulse 1.5s infinite;
}

@keyframes livePulse {
  0%, 100% { opacity: 1; }
  50%       { opacity: 0.4; }
}

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

.log-content         { color: var(--color-green);    word-break: break-all; }
.log-content.log-success { color: var(--color-teal); }
.log-content.log-error   { color: var(--color-red);  }
.log-content.log-tool    { color: var(--color-amber); }
.log-content.log-result  { color: var(--color-text-dim); }
.log-content.log-meta    { color: var(--color-brass-dark); }

.terminal-prompt {
  margin-top: 8px;
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: 2px;
}

.prompt-user   { color: var(--color-green); }
.prompt-sep    { color: var(--color-text-dim); }
.prompt-path   { color: var(--color-blue); }
.prompt-dollar { color: var(--color-text); margin: 0 4px; }

.cursor-blink {
  color: var(--color-amber);
  animation: blink 1s step-end infinite;
}

@keyframes blink {
  0%, 100% { opacity: 1; }
  50%       { opacity: 0; }
}

/* ── Run bar ──────────────────────────────────────────────────── */
.run-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  background: #0c0700;
  border-top: 2px solid #2a1a05;
  flex-shrink: 0;
}

.tool-select {
  background: var(--color-bg);
  border: 2px solid var(--color-brass-dark);
  color: var(--color-amber);
  font-family: var(--font-mono);
  font-size: 11px;
  padding: 5px 6px;
  outline: none;
  cursor: pointer;
  flex-shrink: 0;
}
.tool-select:focus {
  border-color: var(--color-brass);
}

.model-input,
.provider-input {
  background: var(--color-bg);
  border: 2px solid var(--color-brass-dark);
  color: var(--color-text);
  font-family: var(--font-mono);
  font-size: 11px;
  padding: 5px 8px;
  outline: none;
  width: 120px;
  flex-shrink: 0;
}
.model-input:focus,
.provider-input:focus {
  border-color: var(--color-brass);
}
.model-input::placeholder,
.provider-input::placeholder {
  color: var(--color-text-dim);
  font-style: italic;
}

.prompt-input {
  flex: 1;
  background: var(--color-bg);
  border: 2px solid var(--color-brass-dark);
  color: var(--color-text);
  font-family: var(--font-mono);
  font-size: 12px;
  padding: 5px 10px;
  outline: none;
  min-width: 0;
}
.prompt-input:focus {
  border-color: var(--color-brass);
  box-shadow: 0 0 8px rgba(232, 170, 0, 0.25);
}
.prompt-input::placeholder {
  color: var(--color-text-dim);
  font-style: italic;
}
.prompt-input:disabled,
.model-input:disabled,
.provider-input:disabled,
.tool-select:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.run-btn {
  padding: 5px 12px;
  font-size: 10px;
  color: var(--color-green);
  border-color: var(--color-green);
  flex-shrink: 0;
}
.run-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}
.run-btn:not(:disabled):hover {
  background: rgba(0, 255, 136, 0.12);
  box-shadow: 0 0 8px rgba(0, 255, 136, 0.3);
}

.stop-btn {
  padding: 5px 12px;
  font-size: 10px;
  color: var(--color-red);
  border-color: var(--color-red);
  flex-shrink: 0;
  animation: stopPulse 1.2s infinite;
}
.stop-btn:hover {
  background: rgba(255, 51, 51, 0.12);
}

@keyframes stopPulse {
  0%, 100% { box-shadow: 0 0 0 rgba(255,51,51,0); }
  50%       { box-shadow: 0 0 8px rgba(255,51,51,0.4); }
}

.run-error {
  padding: 4px 12px 6px;
  font-size: 11px;
  color: var(--color-red);
  background: rgba(255, 51, 51, 0.07);
  border-top: 1px solid rgba(255, 51, 51, 0.2);
  flex-shrink: 0;
}
</style>
