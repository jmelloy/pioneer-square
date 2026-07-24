<template>
  <div class="actions-panel">
    <!-- Message + shutdown row (always visible) -->
    <div class="action-row">
      <input
        v-model="messageInput"
        class="prompt-input"
        placeholder="Send a message to this worker…"
        @keydown.enter.exact="handleSendMessage"
      />
      <button
        class="pixel-btn icon-btn message-btn"
        :disabled="!messageInput.trim() || sendingMessage"
        @click="handleSendMessage"
        title="Send message to worker"
      >
        {{ sendingMessage ? '…' : '↩' }}
      </button>
      <button
        class="pixel-btn icon-btn shutdown-btn"
        :disabled="workerState === 'offline' || shuttingDown"
        @click="handleShutdown"
        title="Send graceful shutdown signal (operator-initiated)"
      >
        {{ shuttingDown ? '…' : '⏻' }}
      </button>
    </div>

    <div v-if="actionError" class="action-error">{{ actionError }}</div>

    <!-- Expandable advanced actions -->
    <div class="expand-toggle" @click="expanded = !expanded">
      <span class="expand-chevron">{{ expanded ? '▼' : '▶' }}</span>
      <span class="expand-label">MORE ACTIONS</span>
    </div>

    <div v-if="expanded" class="expanded-actions">
      <!-- Assign Task -->
      <div class="action-section">
        <div class="section-header">ASSIGN TASK</div>
        <input
          v-model="assignName"
          class="prompt-input section-input"
          placeholder="Task name (optional)"
        />
        <textarea
          v-model="assignDesc"
          class="prompt-textarea"
          placeholder="Task description…"
          rows="2"
        />
        <div class="section-row">
          <select v-model="assignPhase" class="phase-select">
            <option value="execute">execute</option>
            <option value="plan">plan</option>
            <option value="review">review</option>
            <option value="followup">followup</option>
          </select>
          <button
            class="pixel-btn assign-btn"
            :disabled="!assignDesc.trim() || assigning"
            @click="handleAssignTask"
          >
            {{ assigning ? '…' : '+ Assign' }}
          </button>
        </div>
        <div v-if="assignError" class="action-error">{{ assignError }}</div>
      </div>

      <!-- Send Followup -->
      <div class="action-section">
        <div class="section-header">SEND FOLLOWUP</div>
        <select v-model="followupTaskId" class="task-select">
          <option value="">— select task —</option>
          <option v-for="t in workerTasks" :key="t.id" :value="t.id">
            {{ t.id }}: {{ (t.name || t.description || '').slice(0, 40) }}
          </option>
        </select>
        <textarea
          v-model="followupInstructions"
          class="prompt-textarea"
          placeholder="Follow-up instructions…"
          rows="2"
        />
        <button
          class="pixel-btn followup-btn"
          :disabled="!followupTaskId || !followupInstructions.trim() || sendingFollowup"
          @click="handleSendFollowup"
        >
          {{ sendingFollowup ? '…' : '↺ Send Followup' }}
        </button>
        <div v-if="followupError" class="action-error">{{ followupError }}</div>
      </div>

      <!-- Redirect Task -->
      <div class="action-section">
        <div class="section-header">REDIRECT TASK</div>
        <select v-model="redirectTaskId" class="task-select">
          <option value="">— select task —</option>
          <option v-for="t in activeWorkerTasks" :key="t.id" :value="t.id">
            {{ t.id }}: {{ (t.name || t.description || '').slice(0, 40) }}
          </option>
        </select>
        <textarea
          v-model="redirectInstructions"
          class="prompt-textarea"
          placeholder="New instructions — agent will be interrupted and resumed…"
          rows="2"
        />
        <button
          class="pixel-btn redirect-btn"
          :disabled="!redirectTaskId || !redirectInstructions.trim() || redirecting"
          @click="handleRedirectTask"
        >
          {{ redirecting ? '…' : '→ Redirect' }}
        </button>
        <div v-if="redirectError" class="action-error">{{ redirectError }}</div>
      </div>

      <!-- Cancel Task -->
      <div class="action-section">
        <div class="section-header">CANCEL TASK</div>
        <select v-model="cancelTaskId" class="task-select">
          <option value="">— select task —</option>
          <option v-for="t in cancellableTasks" :key="t.id" :value="t.id">
            {{ t.id }}: {{ (t.name || t.description || '').slice(0, 40) }}
          </option>
        </select>
        <button
          class="pixel-btn cancel-btn"
          :disabled="!cancelTaskId || cancelling"
          @click="handleCancelTask"
        >
          {{ cancelling ? '…' : '✕ Cancel Task' }}
        </button>
        <div v-if="cancelError" class="action-error">{{ cancelError }}</div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useAgentsStore } from '../../stores/agents'
import { useGuildStore } from '../../stores/guild'
import { useTasksStore } from '../../stores/tasks'
import { api } from '../../utils/api'

const props = defineProps<{ workerId: string; workerState?: string }>()

const agentsStore = useAgentsStore()
const guildStore = useGuildStore()
const tasksStore = useTasksStore()

// Basic message / shutdown
const messageInput = ref('')
const sendingMessage = ref(false)
const shuttingDown = ref(false)
const actionError = ref('')

// Expand toggle
const expanded = ref(false)

// Assign task
const assignName = ref('')
const assignDesc = ref('')
const assignPhase = ref('execute')
const assigning = ref(false)
const assignError = ref('')

// Followup
const followupTaskId = ref('')
const followupInstructions = ref('')
const sendingFollowup = ref(false)
const followupError = ref('')

// Redirect
const redirectTaskId = ref('')
const redirectInstructions = ref('')
const redirecting = ref(false)
const redirectError = ref('')

// Cancel
const cancelTaskId = ref('')
const cancelling = ref(false)
const cancelError = ref('')

const TERMINAL_STATES = new Set(['done', 'failed', 'cancelled', 'error'])

const workerTasks = computed(() =>
  tasksStore.liveTasks.filter((t) => t.worker_id === props.workerId),
)

const activeWorkerTasks = computed(() =>
  workerTasks.value.filter((t) => !TERMINAL_STATES.has(t.state)),
)

const cancellableTasks = computed(() =>
  workerTasks.value.filter(
    (t) => t.state === 'pending' || t.state === 'working' || t.state === 'awaiting-review',
  ),
)

function _injectChat(content: string) {
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  api(`/guilds/${guildId}/messages`, {
    method: 'POST',
    json: { from_agent: 'system', to_agent: 'user', content },
  }).catch((e) => console.warn('[WorkerActions] failed to inject chat:', e))
}

function _workerLabel() {
  return agentsStore.workerDisplayName(props.workerId)
}

async function handleSendMessage() {
  const msg = messageInput.value.trim()
  if (!msg || sendingMessage.value) return
  sendingMessage.value = true
  actionError.value = ''
  try {
    await agentsStore.messageWorker(props.workerId, msg)
    _injectChat(`[Foreman] Sent message to ${_workerLabel()}: '${msg}'`)
    messageInput.value = ''
  } catch (e: unknown) {
    actionError.value = e instanceof Error ? e.message : String(e)
  } finally {
    sendingMessage.value = false
  }
}

async function handleShutdown() {
  if (shuttingDown.value) return
  if (!window.confirm(`Send shutdown signal to ${_workerLabel()}? This cannot be undone.`)) return
  shuttingDown.value = true
  actionError.value = ''
  try {
    await agentsStore.shutdownWorker(props.workerId)
    _injectChat(`[Foreman] Sent shutdown signal to ${props.workerId} (operator-initiated).`)
  } catch (e: unknown) {
    actionError.value = e instanceof Error ? e.message : String(e)
  } finally {
    shuttingDown.value = false
  }
}

async function handleAssignTask() {
  const desc = assignDesc.value.trim()
  if (!desc || assigning.value) return
  assigning.value = true
  assignError.value = ''
  try {
    await agentsStore.assignTask(props.workerId, {
      description: desc,
      name: assignName.value.trim() || null,
      phase: assignPhase.value,
    })
    const label = (assignName.value.trim() || desc).slice(0, 60)
    _injectChat(`[Foreman] Assigned task to ${_workerLabel()}: '${label}'`)
    assignName.value = ''
    assignDesc.value = ''
    assignPhase.value = 'execute'
  } catch (e: unknown) {
    assignError.value = e instanceof Error ? e.message : String(e)
  } finally {
    assigning.value = false
  }
}

async function handleSendFollowup() {
  const taskId = followupTaskId.value
  const instructions = followupInstructions.value.trim()
  if (!taskId || !instructions || sendingFollowup.value) return
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  sendingFollowup.value = true
  followupError.value = ''
  try {
    await tasksStore.sendFollowup(guildId, taskId, instructions)
    _injectChat(
      `[Foreman] Sent followup to ${_workerLabel()} on task ${taskId}: '${instructions.slice(0, 80)}'`,
    )
    followupInstructions.value = ''
    followupTaskId.value = ''
  } catch (e: unknown) {
    followupError.value = e instanceof Error ? e.message : String(e)
  } finally {
    sendingFollowup.value = false
  }
}

async function handleRedirectTask() {
  const taskId = redirectTaskId.value
  const instructions = redirectInstructions.value.trim()
  if (!taskId || !instructions || redirecting.value) return
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  redirecting.value = true
  redirectError.value = ''
  try {
    await tasksStore.redirectTask(guildId, taskId, instructions)
    _injectChat(
      `[Foreman] Redirected task ${taskId} on ${_workerLabel()}: '${instructions.slice(0, 80)}'`,
    )
    redirectInstructions.value = ''
    redirectTaskId.value = ''
  } catch (e: unknown) {
    redirectError.value = e instanceof Error ? e.message : String(e)
  } finally {
    redirecting.value = false
  }
}

async function handleCancelTask() {
  const taskId = cancelTaskId.value
  if (!taskId || cancelling.value) return
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  cancelling.value = true
  cancelError.value = ''
  try {
    await tasksStore.cancelTask(guildId, taskId)
    _injectChat(`[Foreman] Cancelled task ${taskId} on ${_workerLabel()} (operator-initiated).`)
    cancelTaskId.value = ''
  } catch (e: unknown) {
    cancelError.value = e instanceof Error ? e.message : String(e)
  } finally {
    cancelling.value = false
  }
}

watch(
  () => props.workerId,
  () => {
    expanded.value = false
    actionError.value = ''
    followupTaskId.value = ''
    followupInstructions.value = ''
    redirectTaskId.value = ''
    redirectInstructions.value = ''
    cancelTaskId.value = ''
  },
)
</script>

<style scoped>
.actions-panel {
  flex-shrink: 0;
  padding: 8px 12px;
  background: #0c0700;
  border-top: 2px solid #2a1a05;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.action-row {
  display: flex;
  align-items: center;
  gap: 6px;
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

.section-input {
  width: 100%;
  flex: unset;
  box-sizing: border-box;
}

.prompt-textarea {
  width: 100%;
  background: var(--color-bg);
  border: 2px solid var(--color-brass-dark);
  color: var(--color-text);
  font-family: var(--font-mono);
  font-size: 12px;
  padding: 5px 10px;
  outline: none;
  resize: none;
  box-sizing: border-box;
}
.prompt-textarea:focus {
  border-color: var(--color-brass);
  box-shadow: 0 0 8px rgba(232, 170, 0, 0.2);
}
.prompt-textarea::placeholder {
  color: var(--color-text-dim);
  font-style: italic;
}

.icon-btn {
  padding: 5px 10px;
  font-size: 12px;
  flex-shrink: 0;
  min-width: 32px;
}

.message-btn {
  color: var(--color-teal);
  border-color: var(--color-teal);
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
  color: var(--color-red);
  border-color: var(--color-red);
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
  padding: 2px 0 0;
  font-size: 11px;
  color: var(--color-red);
}

/* Expand toggle */
.expand-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  padding: 3px 0;
  user-select: none;
  opacity: 0.6;
  transition: opacity 0.15s;
}
.expand-toggle:hover {
  opacity: 1;
}
.expand-chevron {
  font-size: 8px;
  color: var(--color-teal);
}
.expand-label {
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 2px;
  color: var(--color-teal);
}

/* Expanded action sections */
.expanded-actions {
  display: flex;
  flex-direction: column;
  gap: 8px;
  border-top: 1px solid #1a0f02;
  padding-top: 8px;
}

.action-section {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 8px 10px;
  background: rgba(255, 255, 255, 0.02);
  border: 1px solid #1e1205;
}

.section-header {
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 2px;
  color: var(--color-brass-dark);
  margin-bottom: 2px;
}

.section-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.task-select,
.phase-select {
  width: 100%;
  background: var(--color-bg);
  border: 2px solid var(--color-brass-dark);
  color: var(--color-text);
  font-family: var(--font-mono);
  font-size: 11px;
  padding: 4px 8px;
  outline: none;
  box-sizing: border-box;
}
.phase-select {
  flex: 1;
}
.task-select:focus,
.phase-select:focus {
  border-color: var(--color-brass);
}

.assign-btn {
  color: var(--color-teal);
  border-color: var(--color-teal);
  font-size: 9px;
  padding: 4px 10px;
  flex-shrink: 0;
}
.assign-btn:not(:disabled):hover {
  background: rgba(0, 187, 170, 0.12);
  box-shadow: 0 0 8px rgba(0, 187, 170, 0.3);
}
.assign-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}

.followup-btn {
  color: var(--color-amber);
  border-color: var(--color-amber);
  font-size: 9px;
  padding: 4px 10px;
  align-self: flex-start;
}
.followup-btn:not(:disabled):hover {
  background: rgba(255, 204, 0, 0.12);
  box-shadow: 0 0 8px rgba(255, 204, 0, 0.3);
}
.followup-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}

.redirect-btn {
  color: var(--color-blue);
  border-color: var(--color-blue);
  font-size: 9px;
  padding: 4px 10px;
  align-self: flex-start;
}
.redirect-btn:not(:disabled):hover {
  background: rgba(80, 120, 255, 0.12);
  box-shadow: 0 0 8px rgba(80, 120, 255, 0.3);
}
.redirect-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}

.cancel-btn {
  color: var(--color-red);
  border-color: var(--color-red);
  font-size: 9px;
  padding: 4px 10px;
  align-self: flex-start;
}
.cancel-btn:not(:disabled):hover {
  background: rgba(255, 51, 51, 0.12);
  box-shadow: 0 0 8px rgba(255, 51, 51, 0.3);
}
.cancel-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}
</style>
