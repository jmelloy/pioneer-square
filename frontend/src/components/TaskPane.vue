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
      <div v-for="(entry, i) in logs" :key="i" class="log-line">
        <span class="log-ts">{{ formatTs(entry.timestamp) }}</span>
        <span class="log-text" :class="lineClass(entry.line)">{{ entry.line }}</span>
      </div>
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

<script setup>
import { ref, computed, watch, nextTick, onMounted } from 'vue'
import { useTasksStore } from '../stores/tasks.js'
import { useGuildStore } from '../stores/guild.js'

const props = defineProps({ taskId: String })

const tasksStore = useTasksStore()
const guildStore = useGuildStore()

const logsEl = ref(null)
const followupText = ref('')

const task = computed(() => tasksStore.tasks.find(t => t.id === props.taskId) || {})
const logs = computed(() => tasksStore.taskLogs[props.taskId] || [])

const stateClass = computed(() => `state-${(task.value.state || 'pending').replace(/[^a-z]/g, '-')}`)

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

function lineClass(line) {
  if (!line) return ''
  if (line.startsWith('✓') || line.includes('Done')) return 'log-success'
  if (line.startsWith('✗') || line.includes('error') || line.includes('Error')) return 'log-error'
  if (line.startsWith('[worker]')) return 'log-worker'
  if (line.startsWith('▶')) return 'log-tool'
  if (line.startsWith('  →')) return 'log-result'
  if (line.startsWith('[thinking]')) return 'log-thinking'
  return ''
}

function formatTime(iso) {
  if (!iso) return ''
  return new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function formatTs(iso) {
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

.log-line {
  display: flex;
  gap: 10px;
  align-items: baseline;
}

.log-ts {
  font-size: 9px;
  color: var(--color-text-dim);
  flex-shrink: 0;
  opacity: 0.6;
}

.log-text { word-break: break-all; }
.log-success { color: var(--color-green); }
.log-error { color: var(--color-red); }
.log-worker { color: var(--color-brass); }
.log-tool { color: var(--color-teal); }
.log-result { color: var(--color-text-dim); }
.log-thinking { color: var(--color-blue); font-style: italic; }

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
