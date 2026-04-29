<template>
  <div class="task-pane">
    <div class="task-header">
      <div class="task-title-row">
        <span class="task-phase-badge" :class="task.phase">{{ task.phase || 'execute' }}</span>
        <span class="task-name">{{ task.name || task.description?.slice(0, 60) }}</span>
        <span class="task-state-badge" :class="stateClass">{{ tasksStore.stateLabel(task.state) }}</span>
      </div>
      <div class="task-meta">
        <span class="task-id">{{ task.id }}</span>
        <span v-if="task.branch" class="task-branch">⌥ {{ task.branch }}</span>
        <a v-if="task.pr_url" :href="task.pr_url" target="_blank" rel="noopener" class="pr-link">PR →</a>
        <span class="task-time">{{ formatTime(task.created_at) }}</span>
      </div>
    </div>

    <div class="task-logs" ref="logsEl">
      <div v-if="!logs.length" class="logs-empty">No logs yet — waiting for worker output…</div>
      <div v-for="(entry, i) in logs" :key="i" class="log-entry">
        <div
          class="log-line"
          :class="{ 'log-line--expandable': !!entry.detail }"
          @click="entry.detail && toggleDetail(i)"
        >
          <span class="log-ts">{{ formatTs(entry.timestamp) }}</span>
          <span class="log-text" :class="lineClass(entry.line)">{{ entry.line }}</span>
          <span v-if="entry.detail" class="log-expand-icon">{{ expandedIdx === i ? '▲' : '▼' }}</span>
        </div>
        <div v-if="entry.detail && expandedIdx === i" class="log-detail">
          <template v-if="entry.detail.toolType === 'tool_use'">
            <template v-if="entry.detail.name === 'Edit'">
              <div class="log-detail-label">OLD</div>
              <pre class="log-detail-body log-detail-old">{{ entry.detail.input?.old_string }}</pre>
              <div class="log-detail-label log-detail-label--new">NEW</div>
              <pre class="log-detail-body log-detail-new">{{ entry.detail.input?.new_string }}</pre>
            </template>
            <template v-else-if="entry.detail.name === 'Write'">
              <div class="log-detail-label">{{ entry.detail.input?.file_path }}</div>
              <pre class="log-detail-body">{{ entry.detail.input?.content }}</pre>
            </template>
            <template v-else-if="entry.detail.name === 'Bash'">
              <div class="log-detail-label">COMMAND</div>
              <pre class="log-detail-body">{{ entry.detail.input?.command }}</pre>
            </template>
            <template v-else>
              <div class="log-detail-label">{{ entry.detail.name }}</div>
              <pre class="log-detail-body">{{ JSON.stringify(entry.detail.input, null, 2) }}</pre>
            </template>
          </template>
          <template v-else-if="entry.detail.toolType === 'tool_result'">
            <div class="log-detail-label">OUTPUT</div>
            <pre class="log-detail-body">{{ entry.detail.output }}</pre>
          </template>
        </div>
      </div>
    </div>

    <!-- Redirect panel — shown when task is actively working -->
    <div v-if="task.state === 'working' && isWorkerTask" class="redirect-panel">
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
        <button class="pixel-btn redirect-btn" @click="sendRedirect" :disabled="!redirectText.trim() || redirecting">
          {{ redirecting ? '…' : '↩ Redirect' }}
        </button>
        <button class="pixel-btn cancel-task-btn" @click="cancelTask" :disabled="cancelling">
          {{ cancelling ? '…' : '✕ Cancel' }}
        </button>
      </div>
    </div>

    <!-- Cancel button — shown for pending/awaiting-review worker tasks -->
    <div v-if="canCancel && task.state !== 'working'" class="cancel-panel">
      <button class="pixel-btn cancel-task-btn" @click="cancelTask" :disabled="cancelling">
        {{ cancelling ? '…' : '✕ Cancel Task' }}
      </button>
    </div>

    <!-- Follow-up panel — shown when task is awaiting review -->
    <div v-if="task.state === 'awaiting-review'" class="followup-panel">
      <div class="followup-header">FOREMAN FOLLOW-UP</div>
      <div class="followup-row">
        <textarea
          v-model="followupText"
          class="followup-input"
          placeholder="Additional instructions (e.g. 'update tests', 'add docstrings')…"
          rows="2"
          @keydown.ctrl.enter="sendFollowup"
        />
      </div>
      <div class="followup-actions">
        <button class="pixel-btn followup-btn" @click="sendFollowup" :disabled="!followupText.trim()">
          ↺ Follow-up
        </button>
        <button class="pixel-btn finalize-btn" @click="finalizeTask">
          ✓ Finalize
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted } from 'vue'
import { useTasksStore } from '../stores/tasks'
import { useGuildStore } from '../stores/guild'
import type { Task } from '../types'

const props = defineProps<{ taskId: string }>()

const tasksStore = useTasksStore()
const guildStore = useGuildStore()

const logsEl = ref<HTMLElement | null>(null)
const followupText = ref('')
const redirectText = ref('')
const cancelling = ref(false)
const redirecting = ref(false)
const expandedIdx = ref<number | null>(null)

const task = computed<Partial<Task>>(() => tasksStore.tasks.find(t => t.id === props.taskId) || ({} as Partial<Task>))
const logs = computed(() => tasksStore.taskLogs[props.taskId] || [])

const stateClass = computed(() => `state-${(task.value.state || 'pending').replace(/[^a-z]/g, '-')}`)

const isWorkerTask = computed(() => task.value.worker_id && task.value.worker_id !== 'foreman')

const canCancel = computed(() => {
  const s = task.value.state
  return isWorkerTask.value && (s === 'pending' || s === 'working' || s === 'awaiting-review')
})

onMounted(async () => {
  const guildId = guildStore.currentGuild?.id
  if (guildId && props.taskId && !tasksStore.taskLogs[props.taskId]) {
    await tasksStore.fetchTaskLogs(guildId, props.taskId)
  }
})

watch(logs, async () => {
  await nextTick()
  if (logsEl.value) logsEl.value.scrollTop = logsEl.value.scrollHeight
}, { deep: true })

async function sendFollowup() {
  const text = followupText.value.trim()
  if (!text) return
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  try {
    await tasksStore.sendFollowup(guildId, props.taskId, text)
    followupText.value = ''
  } catch (e) {
    console.error('Follow-up failed', e)
  }
}



async function finalizeTask() {
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  try {
    await tasksStore.finalizeTask(guildId, props.taskId)
  } catch (e) {
    console.error('Finalize failed', e)
  }
}

async function cancelTask() {
  const guildId = guildStore.currentGuild?.id
  if (!guildId || cancelling.value) return
  cancelling.value = true
  try {
    await tasksStore.cancelTask(guildId, props.taskId)
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
    await tasksStore.redirectTask(guildId, props.taskId, text)
    redirectText.value = ''
  } catch (e) {
    console.error('Redirect failed', e)
  } finally {
    redirecting.value = false
  }
}

function toggleDetail(i: number) {
  expandedIdx.value = expandedIdx.value === i ? null : i
}

function lineClass(line: string) {
  if (!line) return ''
  if (line.startsWith('✓') || line.includes('Done')) return 'log-success'
  if (line.startsWith('✗') || line.includes('error') || line.includes('Error')) return 'log-error'
  if (line.startsWith('[worker]')) return 'log-worker'
  if (line.startsWith('▶')) return 'log-tool'
  if (line.startsWith('  →')) return 'log-result'
  if (line.startsWith('[thinking]')) return 'log-thinking'
  return ''
}

function formatTime(iso?: string) {
  if (!iso) return ''
  return new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function formatTs(iso?: string) {
  if (!iso) return ''
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}
</script>

<style scoped>
.task-pane {
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--color-bg);
}

.task-header {
  padding: 10px 14px 8px;
  border-bottom: 2px solid var(--color-brass-dark);
  background: var(--color-bg-secondary);
  flex-shrink: 0;
}

.task-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 5px;
}

.task-name {
  font-size: 13px;
  color: var(--color-text);
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.task-phase-badge {
  font-family: var(--font-pixel);
  font-size: 6px;
  padding: 2px 5px;
  border: 1px solid;
  text-transform: uppercase;
  letter-spacing: 1px;
  flex-shrink: 0;
}
.task-phase-badge.plan { color: var(--color-blue); border-color: var(--color-blue); }
.task-phase-badge.execute { color: var(--color-teal); border-color: var(--color-teal); }
.task-phase-badge.review { color: var(--color-amber); border-color: var(--color-amber); }
.task-phase-badge.followup { color: var(--color-orange); border-color: var(--color-orange); }

.task-state-badge {
  font-family: var(--font-pixel);
  font-size: 6px;
  padding: 2px 6px;
  border: 1px solid;
  flex-shrink: 0;
}
.state-pending { color: var(--color-text-dim); border-color: var(--color-text-dim); }
.state-planning { color: var(--color-blue); border-color: var(--color-blue); }
.state-working { color: var(--color-green); border-color: var(--color-green); animation: statePulse 1s infinite; }
.state-awaiting-review { color: var(--color-amber); border-color: var(--color-amber); }
.state-done { color: var(--color-teal); border-color: var(--color-teal); }
.state-failed { color: var(--color-red); border-color: var(--color-red); }
.state-cancelled { color: var(--color-red); border-color: var(--color-red); opacity: 0.7; }
.state-follow-up { color: var(--color-orange); border-color: var(--color-orange); }

@keyframes statePulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

.task-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.task-id {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-text-dim);
}

.task-branch {
  font-size: 10px;
  color: var(--color-teal);
  font-family: var(--font-mono);
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pr-link {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-teal);
  text-decoration: none;
  padding: 2px 6px;
  border: 1px solid var(--color-teal);
}
.pr-link:hover { background: rgba(0,187,170,0.15); }

.task-time {
  font-size: 10px;
  color: var(--color-text-dim);
  margin-left: auto;
}

/* ── Logs ── */
.task-logs {
  flex: 1;
  overflow-y: auto;
  padding: 10px 14px;
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 1.5;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.logs-empty {
  color: var(--color-text-dim);
  font-style: italic;
  margin-top: 20px;
  text-align: center;
}

.log-ts {
  font-size: 9px;
  color: var(--color-text-dim);
  flex-shrink: 0;
  opacity: 0.6;
}

.log-entry { display: flex; flex-direction: column; }

.log-line {
  display: flex;
  gap: 10px;
  align-items: baseline;
}

.log-line--expandable {
  cursor: pointer;
  border-radius: 2px;
}
.log-line--expandable:hover { background: rgba(255,255,255,0.04); }

.log-expand-icon {
  font-size: 8px;
  color: var(--color-text-dim);
  flex-shrink: 0;
  margin-left: auto;
  opacity: 0.5;
}

.log-detail {
  margin: 3px 0 4px 42px;
  border-left: 2px solid var(--color-brass-dark);
  padding-left: 10px;
}

.log-detail-label {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-brass-dark);
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
  background: rgba(0,0,0,0.2);
  border: 1px solid var(--color-brass-dark);
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

.log-text { word-break: break-all; white-space: pre-wrap; }
.log-success { color: var(--color-green); }
.log-error { color: var(--color-red); }
.log-worker { color: var(--color-brass); }
.log-tool { color: var(--color-teal); }
.log-result { color: var(--color-text-dim); }
.log-thinking { color: var(--color-blue); font-style: italic; }

/* ── Redirect panel ── */
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

.redirect-input {
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
  box-shadow: 0 0 8px rgba(80,120,255,0.2);
}
.redirect-input::placeholder { color: var(--color-text-dim); font-style: italic; }

.redirect-actions {
  display: flex;
  gap: 8px;
}

.redirect-btn {
  background: linear-gradient(180deg, rgba(80,120,255,0.2) 0%, rgba(50,80,200,0.3) 100%);
  border-color: var(--color-blue);
  color: var(--color-blue);
  font-size: 8px;
  padding: 5px 12px;
}
.redirect-btn:hover:not(:disabled) {
  background: linear-gradient(180deg, rgba(80,120,255,0.4) 0%, rgba(60,100,220,0.5) 100%);
  box-shadow: 0 0 8px rgba(80,120,255,0.3);
}
.redirect-btn:disabled { opacity: 0.35; cursor: not-allowed; }

/* ── Cancel panel ── */
.cancel-panel {
  flex-shrink: 0;
  padding: 6px 14px;
  border-top: 1px solid var(--color-brass-dark);
  display: flex;
  justify-content: flex-end;
}

.cancel-task-btn {
  background: linear-gradient(180deg, rgba(220,50,50,0.15) 0%, rgba(160,30,30,0.25) 100%);
  border-color: var(--color-red);
  color: var(--color-red);
  font-size: 8px;
  padding: 4px 10px;
}
.cancel-task-btn:hover:not(:disabled) {
  background: linear-gradient(180deg, rgba(220,50,50,0.35) 0%, rgba(180,40,40,0.45) 100%);
  box-shadow: 0 0 8px rgba(220,50,50,0.3);
}
.cancel-task-btn:disabled { opacity: 0.35; cursor: not-allowed; }

/* ── Follow-up panel ── */
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
.followup-input:focus {
  border-color: var(--color-amber);
  box-shadow: 0 0 8px rgba(255,204,0,0.2);
}
.followup-input::placeholder { color: var(--color-text-dim); font-style: italic; }

.followup-actions {
  display: flex;
  gap: 8px;
}

.followup-btn {
  background: linear-gradient(180deg, rgba(255,204,0,0.2) 0%, rgba(180,140,0,0.3) 100%);
  border-color: var(--color-amber);
  color: var(--color-amber);
  font-size: 8px;
  padding: 5px 12px;
}
.followup-btn:hover:not(:disabled) {
  background: linear-gradient(180deg, rgba(255,204,0,0.4) 0%, rgba(200,160,0,0.5) 100%);
  box-shadow: 0 0 8px rgba(255,204,0,0.3);
}
.followup-btn:disabled { opacity: 0.35; cursor: not-allowed; }

.finalize-btn {
  background: linear-gradient(180deg, rgba(0,187,170,0.2) 0%, rgba(0,120,110,0.3) 100%);
  border-color: var(--color-teal);
  color: var(--color-teal);
  font-size: 8px;
  padding: 5px 12px;
}
.finalize-btn:hover {
  background: linear-gradient(180deg, rgba(0,187,170,0.4) 0%, rgba(0,150,140,0.5) 100%);
  box-shadow: 0 0 8px rgba(0,187,170,0.3);
}
</style>
