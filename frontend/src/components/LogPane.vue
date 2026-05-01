<template>
  <div class="log-pane" :class="'kind-' + kind">
    <!-- ── Header ─────────────────────────────────────────── -->
    <div class="pane-header">
      <div class="pane-title">
        <span class="title-icon">{{ icon }}</span>
        <span class="title-text">{{ titleText }}</span>
      </div>
      <div class="pane-meta">
        <template v-if="kind === 'task'">
          <span v-if="taskPhase" class="phase-badge" :class="taskPhase">{{ taskPhase }}</span>
          <span v-if="taskStateLabel" class="state-badge" :class="stateBadgeClass">
            {{ taskStateLabel }}
          </span>
        </template>
        <template v-else>
          <span v-if="entityState" class="state-badge" :class="entityState">{{ entityState }}</span>
          <span class="live-indicator" :class="{ active: isLive }">
            {{ isLive ? '● LIVE' : '○ IDLE' }}
          </span>
        </template>
      </div>
    </div>

    <div
      v-if="kind === 'task' && (taskBranch || taskPrUrl || taskCreatedAt)"
      class="pane-subheader"
    >
      <span v-if="taskId" class="sub-id">{{ taskId }}</span>
      <span v-if="taskBranch" class="sub-branch">⌥ {{ taskBranch }}</span>
      <a v-if="taskPrUrl" :href="taskPrUrl" target="_blank" rel="noopener" class="sub-pr-link"
        >PR →</a
      >
      <span v-if="taskCreatedAt" class="sub-time">{{ formatDateTime(taskCreatedAt) }}</span>
    </div>

    <!-- ── Logs ───────────────────────────────────────────── -->
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
          <span class="log-text" :class="lineClass(log.line)" v-html="linkify(log.line)"></span>
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

    <!-- ── Actions / panels ────────────────────────────────── -->

    <!-- Agent: run command bar -->
    <div v-if="kind === 'agent'" class="actions-panel">
      <div class="actions-header">ACTIONS</div>
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
          class="pixel-btn run-btn"
          :disabled="!runPrompt.trim()"
          @click="handleRun"
          title="Run agent"
        >
          ▶ RUN
        </button>
        <button v-else class="pixel-btn stop-btn" @click="handleStop" title="Stop agent">
          ■ STOP
        </button>
      </div>
      <div v-if="runError" class="action-error">{{ runError }}</div>
    </div>

    <!-- Worker: action buttons -->
    <div v-if="kind === 'worker'" class="actions-panel">
      <div class="actions-header">ACTIONS</div>
      <div v-if="pendingAuthUrl" class="auth-banner">
        <span class="auth-icon">⚿</span>
        <span class="auth-text">Authentication required —</span>
        <a :href="pendingAuthUrl" target="_blank" rel="noopener" class="auth-link">{{
          pendingAuthUrl
        }}</a>
      </div>
      <div class="action-row">
        <input
          v-model="messageInput"
          class="prompt-input"
          placeholder="Send a message to this worker…"
          @keydown.enter.exact="handleSendMessage"
        />
        <button
          class="pixel-btn message-btn"
          :disabled="!messageInput.trim() || sendingMessage"
          @click="handleSendMessage"
          title="Send message to worker"
        >
          {{ sendingMessage ? '…' : '↩ Send' }}
        </button>
        <button
          class="pixel-btn shutdown-btn"
          :disabled="entityState === 'offline' || shuttingDown"
          @click="handleShutdown"
          title="Ask the worker to shut down"
        >
          {{ shuttingDown ? '…' : '⏻ Shut down' }}
        </button>
      </div>
      <div v-if="actionError" class="action-error">{{ actionError }}</div>
    </div>

    <!-- Task: redirect panel (active during work) -->
    <div v-if="kind === 'task' && taskState === 'working' && isWorkerTask" class="redirect-panel">
      <div class="redirect-header">REDIRECT AGENT</div>
      <div class="redirect-row">
        <textarea
          v-model="redirectText"
          class="redirect-input"
          placeholder="New instructions — agent will be interrupted and resumed with full context…"
          rows="2"
          @keydown.ctrl.enter="sendRedirect"
        />
      </div>
      <div class="redirect-actions">
        <button
          class="pixel-btn redirect-btn"
          @click="sendRedirect"
          :disabled="!redirectText.trim() || redirecting"
        >
          {{ redirecting ? '…' : '↩ Redirect' }}
        </button>
        <button class="pixel-btn cancel-task-btn" @click="cancelTaskAction" :disabled="cancelling">
          {{ cancelling ? '…' : '✕ Cancel' }}
        </button>
      </div>
    </div>

    <!-- Task: cancel button for pending/awaiting-review tasks -->
    <div v-if="kind === 'task' && canCancel && taskState !== 'working'" class="cancel-panel">
      <button class="pixel-btn cancel-task-btn" @click="cancelTaskAction" :disabled="cancelling">
        {{ cancelling ? '…' : '✕ Cancel Task' }}
      </button>
    </div>

    <!-- Task: follow-up panel -->
    <div v-if="kind === 'task' && taskState === 'awaiting-review'" class="followup-panel">
      <div class="followup-header">FOREMAN FOLLOW-UP</div>
      <div class="followup-row">
        <textarea
          v-model="followupText"
          class="followup-input"
          placeholder="Additional instructions (e.g. 'update tests', 'add docstrings')…"
          rows="2"
          @keydown.ctrl.enter="sendFollowupAction"
        />
      </div>
      <div class="followup-actions">
        <button
          class="pixel-btn followup-btn"
          @click="sendFollowupAction"
          :disabled="!followupText.trim()"
        >
          ↺ Follow-up
        </button>
        <button class="pixel-btn finalize-btn" @click="finalizeTaskAction">✓ Finalize</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onUnmounted } from 'vue'
import { useAgentsStore } from '../stores/agents'
import { useTasksStore } from '../stores/tasks'
import { useGuildStore } from '../stores/guild'
import { useAuthStore } from '../stores/auth'
import type { LogEntry, Task, WSMessage } from '../types'

type PaneKind = 'agent' | 'worker' | 'task'

const props = defineProps<{
  kind: PaneKind
  id: string
}>()

const API_BASE = (import.meta.env.VITE_API_BASE as string) ?? ''

const agentsStore = useAgentsStore()
const tasksStore = useTasksStore()
const guildStore = useGuildStore()
const authStore = useAuthStore()

const bodyEl = ref<HTMLElement | null>(null)
const expandedIdx = ref<number | null>(null)

// ── Source-of-truth lookups per kind ───────────────────────────────
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

// ── Header data ─────────────────────────────────────────────────────
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

const isLive = computed(() => {
  const s = entityState.value
  return !!s && !['idle', 'offline'].includes(s)
})

// ── Task-only header data ───────────────────────────────────────────
const taskId = computed(() => (props.kind === 'task' ? task.value.id : undefined))
const taskPhase = computed(() =>
  props.kind === 'task' ? task.value.phase || 'execute' : undefined,
)
const taskState = computed(() => task.value.state)
const taskStateLabel = computed(() =>
  taskState.value ? tasksStore.stateLabel(taskState.value) : '',
)
const stateBadgeClass = computed(
  () => `state-${(taskState.value || 'pending').replace(/[^a-z]/g, '-')}`,
)
const taskBranch = computed(() => task.value.branch)
const taskPrUrl = computed(() => task.value.pr_url)
const taskCreatedAt = computed(() => task.value.created_at)

const isWorkerTask = computed(() => task.value.worker_id && task.value.worker_id !== 'foreman')
const canCancel = computed(() => {
  const s = task.value.state
  return isWorkerTask.value && (s === 'pending' || s === 'working' || s === 'awaiting-review')
})

// ── Run/stop (agent kind) ───────────────────────────────────────────
const runTool = ref('claude')
const runPrompt = ref('')
const runModel = ref('')
const runProvider = ref('')
const runError = ref('')
const isRunning = computed(() => ['working', 'thinking', 'busy'].includes(agent.value?.state ?? ''))
const modelPlaceholder = computed(() => {
  if (runTool.value === 'claude') return 'claude-opus-4-7'
  if (runTool.value === 'codex') return 'o4-mini'
  return 'model (optional)'
})

async function handleRun() {
  if (!runPrompt.value.trim() || isRunning.value) return
  runError.value = ''
  try {
    await agentsStore.runAgent(props.id, {
      tool: runTool.value,
      prompt: runPrompt.value.trim(),
      model: runModel.value.trim(),
      provider: runProvider.value.trim(),
    })
    runPrompt.value = ''
  } catch (e: any) {
    runError.value = e.message
  }
}

async function handleStop() {
  runError.value = ''
  try {
    await agentsStore.stopAgent(props.id)
  } catch (e: any) {
    runError.value = e.message
  }
}

// ── Worker actions ──────────────────────────────────────────────────
const messageInput = ref('')
const sendingMessage = ref(false)
const shuttingDown = ref(false)
const actionError = ref('')
const pendingAuthUrl = ref<string | null>(null)

async function handleSendMessage() {
  const msg = messageInput.value.trim()
  if (!msg || sendingMessage.value) return
  sendingMessage.value = true
  actionError.value = ''
  try {
    await agentsStore.messageWorker(props.id, msg)
    messageInput.value = ''
  } catch (e: any) {
    actionError.value = e.message
  } finally {
    sendingMessage.value = false
  }
}

async function handleShutdown() {
  if (shuttingDown.value) return
  shuttingDown.value = true
  actionError.value = ''
  try {
    await agentsStore.messageWorker(
      props.id,
      'Please shut down cleanly: finish any in-flight work and exit the worker process.',
    )
  } catch (e: any) {
    actionError.value = e.message
  } finally {
    shuttingDown.value = false
  }
}

async function fetchPendingAuth() {
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  try {
    const res = await fetch(`${API_BASE}/guilds/${guildId}/pending-auth`, {
      headers: authStore.authHeaders(),
    })
    if (!res.ok) return
    const items: Array<{ workerId: string; url: string }> = await res.json()
    const match = items.find((i) => i.workerId === props.id)
    pendingAuthUrl.value = match?.url ?? null
  } catch {
    // non-fatal
  }
}

function handleAuthEvent(data: WSMessage) {
  if (props.kind !== 'worker') return
  if (data.type === 'claude-auth-required' && data.workerId === props.id) {
    pendingAuthUrl.value = data.url
  } else if (
    (data.type === 'claude-auth-success' || data.type === 'claude-auth-cleared') &&
    data.workerId === props.id
  ) {
    pendingAuthUrl.value = null
  }
}

// ── Task actions ────────────────────────────────────────────────────
const followupText = ref('')
const redirectText = ref('')
const cancelling = ref(false)
const redirecting = ref(false)

async function sendFollowupAction() {
  const text = followupText.value.trim()
  if (!text) return
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  try {
    await tasksStore.sendFollowup(guildId, props.id, text)
    followupText.value = ''
  } catch (e) {
    console.error('Follow-up failed', e)
  }
}

async function finalizeTaskAction() {
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  try {
    await tasksStore.finalizeTask(guildId, props.id)
  } catch (e) {
    console.error('Finalize failed', e)
  }
}

async function cancelTaskAction() {
  const guildId = guildStore.currentGuild?.id
  if (!guildId || cancelling.value) return
  cancelling.value = true
  try {
    await tasksStore.cancelTask(guildId, props.id)
  } catch (e) {
    console.error('Cancel failed', e)
  } finally {
    cancelling.value = false
  }
}

async function sendRedirect() {
  const text = redirectText.value.trim()
  if (!text || redirecting.value) return
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  redirecting.value = true
  try {
    await tasksStore.redirectTask(guildId, props.id, text)
    redirectText.value = ''
  } catch (e) {
    console.error('Redirect failed', e)
  } finally {
    redirecting.value = false
  }
}

// ── Initial log fetch ───────────────────────────────────────────────
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

onMounted(async () => {
  await loadLogs()
  if (props.kind === 'worker') {
    await fetchPendingAuth()
    guildStore.addMessageHandler(handleAuthEvent)
  }
})

onUnmounted(() => {
  if (props.kind === 'worker') {
    guildStore.removeMessageHandler(handleAuthEvent)
  }
})

// Re-fetch when the id or kind changes (tab swaps re-use the component instance)
watch(
  () => [props.kind, props.id],
  async () => {
    expandedIdx.value = null
    pendingAuthUrl.value = null
    await loadLogs()
    if (props.kind === 'worker') await fetchPendingAuth()
  },
)

watch(
  logs,
  async () => {
    await nextTick()
    if (bodyEl.value) bodyEl.value.scrollTop = bodyEl.value.scrollHeight
  },
  { deep: true },
)

// ── Helpers ─────────────────────────────────────────────────────────
function toggleDetail(i: number) {
  expandedIdx.value = expandedIdx.value === i ? null : i
}

function formatTime(iso?: string) {
  if (!iso) return '00:00:00'
  return new Date(iso).toLocaleTimeString('en-US', { hour12: false })
}

function formatDateTime(iso?: string) {
  if (!iso) return ''
  return new Date(iso).toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

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

/* ── Header ─────────────────────────────────────────────── */
.pane-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 16px;
  background: #0f0a02;
  border-bottom: 2px solid #2a1a05;
  flex-shrink: 0;
}

.pane-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}

.kind-agent .pane-title {
  color: var(--color-amber);
}
.kind-worker .pane-title {
  color: var(--color-teal);
}
.kind-task .pane-title {
  color: var(--color-text);
}

.title-icon {
  color: var(--color-green);
}
.kind-worker .title-icon {
  color: var(--color-teal);
}
.kind-task .title-icon {
  color: var(--color-brass);
}

.pane-meta {
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
.state-badge.idle {
  background: rgba(154, 128, 96, 0.2);
  color: var(--color-text-dim);
}
.state-badge.thinking {
  background: rgba(68, 153, 255, 0.2);
  color: var(--color-blue);
}
.state-badge.working {
  background: rgba(0, 255, 136, 0.2);
  color: var(--color-green);
}
.state-badge.busy {
  background: rgba(255, 136, 68, 0.2);
  color: var(--color-orange);
}
.state-badge.error {
  background: rgba(255, 51, 51, 0.2);
  color: var(--color-red);
}
.state-badge.offline {
  background: rgba(100, 100, 100, 0.2);
  color: var(--color-text-dim);
}

.live-indicator {
  font-size: 11px;
  color: var(--color-text-dim);
}
.kind-agent .live-indicator.active {
  color: var(--color-green);
  animation: livePulse 1.5s infinite;
}
.kind-worker .live-indicator.active {
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

.kind-task .state-badge {
  font-size: 6px;
  padding: 2px 6px;
  background: none;
  border: 1px solid;
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

/* Task subheader */
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

/* ── Body / logs ─────────────────────────────────────────── */
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

/* ── Generic actions panel (agent + worker) ───────────────── */
.actions-panel {
  flex-shrink: 0;
  padding: 8px 12px;
  background: #0c0700;
  border-top: 2px solid #2a1a05;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.actions-header {
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 2px;
  color: var(--color-brass-dark);
}
.kind-agent .actions-header {
  color: var(--color-amber);
}
.kind-worker .actions-header {
  color: var(--color-teal);
}

.run-bar,
.action-row {
  display: flex;
  align-items: center;
  gap: 6px;
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
  0%,
  100% {
    box-shadow: 0 0 0 rgba(255, 51, 51, 0);
  }
  50% {
    box-shadow: 0 0 8px rgba(255, 51, 51, 0.4);
  }
}

.message-btn {
  padding: 5px 12px;
  font-size: 10px;
  color: var(--color-teal);
  border-color: var(--color-teal);
  flex-shrink: 0;
}
.message-btn:not(:disabled):hover {
  background: rgba(0, 187, 170, 0.12);
  box-shadow: 0 0 8px rgba(0, 187, 170, 0.3);
}
.message-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}

.shutdown-btn {
  padding: 5px 12px;
  font-size: 10px;
  color: var(--color-red);
  border-color: var(--color-red);
  flex-shrink: 0;
}
.shutdown-btn:not(:disabled):hover {
  background: rgba(255, 51, 51, 0.12);
  box-shadow: 0 0 8px rgba(255, 51, 51, 0.3);
}
.shutdown-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}

.action-error {
  padding: 4px 0 0;
  font-size: 11px;
  color: var(--color-red);
}

.auth-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  background: rgba(255, 204, 0, 0.08);
  border: 1px solid var(--color-amber);
  border-radius: 2px;
  font-size: 11px;
  color: var(--color-amber);
  flex-wrap: wrap;
}
.auth-icon {
  font-size: 14px;
}
.auth-text {
  font-family: var(--font-pixel);
  font-size: 7px;
  letter-spacing: 1px;
}
.auth-link {
  color: var(--color-teal);
  text-decoration: underline;
  word-break: break-all;
  font-size: 11px;
}

/* ── Task action panels ───────────────────────────────────── */
.redirect-panel {
  flex-shrink: 0;
  border-top: 2px solid var(--color-blue);
  background: rgba(80, 120, 255, 0.04);
  padding: 10px 14px;
}

.redirect-header {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-blue);
  letter-spacing: 2px;
  margin-bottom: 8px;
}

.redirect-row {
  margin-bottom: 8px;
}

.redirect-input,
.followup-input {
  width: 100%;
  background: var(--color-bg);
  border: 2px solid var(--color-brass-dark);
  color: var(--color-text);
  font-family: var(--font-mono);
  font-size: 12px;
  padding: 6px 10px;
  outline: none;
  resize: none;
  box-sizing: border-box;
}
.redirect-input:focus {
  border-color: var(--color-blue);
  box-shadow: 0 0 8px rgba(80, 120, 255, 0.2);
}
.followup-input:focus {
  border-color: var(--color-amber);
  box-shadow: 0 0 8px rgba(255, 204, 0, 0.2);
}
.redirect-input::placeholder,
.followup-input::placeholder {
  color: var(--color-text-dim);
  font-style: italic;
}

.redirect-actions,
.followup-actions {
  display: flex;
  gap: 8px;
}

.redirect-btn {
  background: linear-gradient(180deg, rgba(80, 120, 255, 0.2) 0%, rgba(50, 80, 200, 0.3) 100%);
  border-color: var(--color-blue);
  color: var(--color-blue);
  font-size: 8px;
  padding: 5px 12px;
}
.redirect-btn:hover:not(:disabled) {
  background: linear-gradient(180deg, rgba(80, 120, 255, 0.4) 0%, rgba(60, 100, 220, 0.5) 100%);
  box-shadow: 0 0 8px rgba(80, 120, 255, 0.3);
}
.redirect-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}

.cancel-panel {
  flex-shrink: 0;
  padding: 6px 14px;
  border-top: 1px solid var(--color-brass-dark);
  display: flex;
  justify-content: flex-end;
}

.cancel-task-btn {
  background: linear-gradient(180deg, rgba(220, 50, 50, 0.15) 0%, rgba(160, 30, 30, 0.25) 100%);
  border-color: var(--color-red);
  color: var(--color-red);
  font-size: 8px;
  padding: 4px 10px;
}
.cancel-task-btn:hover:not(:disabled) {
  background: linear-gradient(180deg, rgba(220, 50, 50, 0.35) 0%, rgba(180, 40, 40, 0.45) 100%);
  box-shadow: 0 0 8px rgba(220, 50, 50, 0.3);
}
.cancel-task-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}

.followup-panel {
  flex-shrink: 0;
  border-top: 2px solid var(--color-amber);
  background: rgba(255, 204, 0, 0.04);
  padding: 10px 14px;
}

.followup-header {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-amber);
  letter-spacing: 2px;
  margin-bottom: 8px;
}

.followup-row {
  margin-bottom: 8px;
}

.followup-btn {
  background: linear-gradient(180deg, rgba(255, 204, 0, 0.2) 0%, rgba(180, 140, 0, 0.3) 100%);
  border-color: var(--color-amber);
  color: var(--color-amber);
  font-size: 8px;
  padding: 5px 12px;
}
.followup-btn:hover:not(:disabled) {
  background: linear-gradient(180deg, rgba(255, 204, 0, 0.4) 0%, rgba(200, 160, 0, 0.5) 100%);
  box-shadow: 0 0 8px rgba(255, 204, 0, 0.3);
}
.followup-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}

.finalize-btn {
  background: linear-gradient(180deg, rgba(0, 187, 170, 0.2) 0%, rgba(0, 120, 110, 0.3) 100%);
  border-color: var(--color-teal);
  color: var(--color-teal);
  font-size: 8px;
  padding: 5px 12px;
}
.finalize-btn:hover {
  background: linear-gradient(180deg, rgba(0, 187, 170, 0.4) 0%, rgba(0, 150, 140, 0.5) 100%);
  box-shadow: 0 0 8px rgba(0, 187, 170, 0.3);
}
</style>
