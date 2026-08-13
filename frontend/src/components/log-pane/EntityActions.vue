<template>
  <!-- Agent: run bar for starting an interactive task -->
  <div v-if="kind === 'agent'" class="actions-panel">
    <div class="actions-header">ACTIONS</div>
    <div class="run-bar">
      <select v-model="runTool" class="tool-select" :disabled="isRunning" @change="runModel = ''">
        <option value="claude">claude</option>
        <option value="codex">codex</option>
        <option value="pi">pi</option>
      </select>
      <select
        v-if="toolModels.length"
        v-model="runModel"
        class="model-input"
        :disabled="isRunning"
        title="Model (optional)"
      >
        <option value="">{{ modelPlaceholder }}</option>
        <option v-for="m in toolModels" :key="m.id" :value="m.id">{{ m.name }}</option>
      </select>
      <input
        v-else
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
        :placeholder="promptPlaceholder"
        :disabled="isRunning"
        @keydown.enter.exact="handleRun"
      />
      <button
        v-if="!isRunning"
        class="pixel-btn run-btn"
        :disabled="!runPrompt.trim()"
        @click="handleRun"
        title="Start interactive task"
      >
        ▶ RUN
      </button>
      <button v-else class="pixel-btn stop-btn" @click="handleStop" title="Stop agent">
        ■ STOP
      </button>
    </div>
    <div v-if="runError" class="action-error">{{ runError }}</div>
  </div>

  <!-- Worker: message/shutdown + expandable advanced actions -->
  <div v-if="kind === 'worker'" class="actions-panel">
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

    <div class="expand-toggle" @click="expanded = !expanded">
      <span class="expand-chevron">{{ expanded ? '▼' : '▶' }}</span>
      <span class="expand-label">MORE ACTIONS</span>
    </div>

    <div v-if="expanded" class="expanded-actions">
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
          class="pixel-btn worker-followup-btn"
          :disabled="!followupTaskId || !followupInstructions.trim() || sendingFollowup"
          @click="handleSendFollowup"
        >
          {{ sendingFollowup ? '…' : '↺ Send Followup' }}
        </button>
        <div v-if="followupError" class="action-error">{{ followupError }}</div>
      </div>

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
          class="pixel-btn worker-redirect-btn"
          :disabled="!redirectTaskId || !redirectInstructions.trim() || workerRedirecting"
          @click="handleRedirectTask"
        >
          {{ workerRedirecting ? '…' : '→ Redirect' }}
        </button>
        <div v-if="workerRedirectError" class="action-error">{{ workerRedirectError }}</div>
      </div>

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
          :disabled="!cancelTaskId || workerCancelling"
          @click="handleCancelTask"
        >
          {{ workerCancelling ? '…' : '✕ Cancel Task' }}
        </button>
        <div v-if="workerCancelError" class="action-error">{{ workerCancelError }}</div>
      </div>
    </div>
  </div>

  <!-- Task: redirect/cancel/followup panels — used for the task pane itself,
         and embedded under an agent pane when that agent has an active task. -->
  <template v-if="showTaskPanel">
    <div v-if="isInteractive && taskState === 'working' && isWorkerTask" class="redirect-panel">
      <div class="redirect-header">INTERACTIVE AGENT</div>
      <div class="redirect-row">
        <textarea
          v-model="messageText"
          class="redirect-input"
          placeholder="Send a message to this interactive session…"
          rows="2"
          @keydown.ctrl.enter="sendTaskMessage"
        />
      </div>
      <div class="redirect-actions">
        <button
          class="pixel-btn redirect-btn"
          @click="sendTaskMessage"
          :disabled="!messageText.trim() || messaging"
        >
          {{ messaging ? '…' : '➤ Send' }}
        </button>
        <button
          class="pixel-btn cancel-task-btn"
          @click="cancelTaskAction"
          :disabled="taskCancelling"
        >
          {{ taskCancelling ? '…' : '✕ Close' }}
        </button>
      </div>
      <div v-if="taskCancelError" class="cancel-error redirect-cancel-error">
        {{ taskCancelError }}
      </div>
    </div>

    <div v-if="!isInteractive && taskState === 'working' && isWorkerTask" class="redirect-panel">
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
          :disabled="!redirectText.trim() || taskRedirecting"
        >
          {{ taskRedirecting ? '…' : '↩ Redirect' }}
        </button>
        <button
          class="pixel-btn cancel-task-btn"
          @click="cancelTaskAction"
          :disabled="taskCancelling"
        >
          {{ taskCancelling ? '…' : '✕ Cancel' }}
        </button>
      </div>
      <div v-if="taskCancelError" class="cancel-error redirect-cancel-error">
        {{ taskCancelError }}
      </div>
    </div>

    <div v-if="canCancel && taskState !== 'working'" class="cancel-panel">
      <span v-if="taskCancelError" class="cancel-error">{{ taskCancelError }}</span>
      <button
        class="pixel-btn cancel-task-btn"
        @click="cancelTaskAction"
        :disabled="taskCancelling"
      >
        {{ taskCancelling ? '…' : '✕ Cancel Task' }}
      </button>
    </div>

    <div v-if="showFollowupPanel" class="followup-panel">
      <div class="followup-header">{{ followupPanelHeader }}</div>
      <div class="followup-row">
        <textarea
          v-model="followupText"
          class="followup-input"
          :placeholder="followupPlaceholder"
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
        <button
          v-if="taskState === 'awaiting-review'"
          class="pixel-btn finalize-btn"
          @click="finalizeTaskAction"
        >
          ✓ Finalize
        </button>
      </div>
    </div>
  </template>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { useAgentsStore } from '../../stores/agents'
import { useGuildStore } from '../../stores/guild'
import { useTasksStore } from '../../stores/tasks'
import { useModels } from '../../composables/useModels'
import { api } from '../../utils/api'

type EntityKind = 'agent' | 'worker' | 'task'

const props = defineProps<{
  kind: EntityKind
  id: string
  entityState?: string
  // Task info: the task itself when kind === 'task', or the active task
  // belonging to the agent when kind === 'agent'.
  taskId?: string
  taskState?: string
  taskType?: string
  taskWorkerId?: string
}>()

const agentsStore = useAgentsStore()
const modelsStore = useModels()
const guildStore = useGuildStore()
const tasksStore = useTasksStore()

/* ------------------------------------------------------------------ */
/* Agent: run bar                                                      */
/* ------------------------------------------------------------------ */

const runTool = ref('claude')
const runPrompt = ref('')
const runModel = ref('')
const runProvider = ref('')
const runError = ref('')

const isRunning = computed(() => ['working', 'thinking', 'busy'].includes(props.entityState ?? ''))

const promptPlaceholder = computed(() => `Start an interactive ${runTool.value} task…`)

const modelPlaceholder = computed(() => {
  if (runTool.value === 'claude') return 'model (default)'
  if (runTool.value === 'codex') return 'model (default)'
  return 'model (optional)'
})

const TOOL_PROVIDER: Record<string, string> = {
  claude: 'anthropic',
  codex: 'openai',
}

const toolModels = computed(() => {
  const providerId = TOOL_PROVIDER[runTool.value]
  if (!providerId) return []
  return modelsStore.modelsForProvider(providerId)
})

onMounted(() => {
  if (props.kind === 'agent') modelsStore.loadModels()
})

async function handleRun() {
  if (!runPrompt.value.trim() || isRunning.value) return
  runError.value = ''
  try {
    const result = await agentsStore.runAgent(props.id, {
      tool: runTool.value,
      prompt: runPrompt.value.trim(),
      model: runModel.value.trim(),
      provider: runProvider.value.trim(),
    })
    runPrompt.value = ''
    if (result && typeof result === 'object' && 'taskId' in result) {
      agentsStore.openTaskTab(String(result.taskId))
    }
  } catch (e: unknown) {
    runError.value = e instanceof Error ? e.message : String(e)
  }
}

async function handleStop() {
  runError.value = ''
  try {
    await agentsStore.stopAgent(props.id)
  } catch (e: unknown) {
    runError.value = e instanceof Error ? e.message : String(e)
  }
}

/* ------------------------------------------------------------------ */
/* Worker: message/shutdown + expandable advanced actions              */
/* ------------------------------------------------------------------ */

const workerState = computed(() => props.entityState)

const messageInput = ref('')
const sendingMessage = ref(false)
const shuttingDown = ref(false)
const actionError = ref('')

const expanded = ref(false)

const assignName = ref('')
const assignDesc = ref('')
const assignPhase = ref('execute')
const assigning = ref(false)
const assignError = ref('')

const followupTaskId = ref('')
const followupInstructions = ref('')
const sendingFollowup = ref(false)
const followupError = ref('')

const redirectTaskId = ref('')
const redirectInstructions = ref('')
const workerRedirecting = ref(false)
const workerRedirectError = ref('')

const cancelTaskId = ref('')
const workerCancelling = ref(false)
const workerCancelError = ref('')

const WORKER_TERMINAL_STATES = new Set(['done', 'failed', 'cancelled', 'error'])

const workerTasks = computed(() => tasksStore.liveTasks.filter((t) => t.worker_id === props.id))

const activeWorkerTasks = computed(() =>
  workerTasks.value.filter((t) => !WORKER_TERMINAL_STATES.has(t.state)),
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
  }).catch((e) => console.warn('[EntityActions] failed to inject chat:', e))
}

function _workerLabel() {
  return agentsStore.workerDisplayName(props.id)
}

async function handleSendMessage() {
  const msg = messageInput.value.trim()
  if (!msg || sendingMessage.value) return
  sendingMessage.value = true
  actionError.value = ''
  try {
    await agentsStore.messageWorker(props.id, msg)
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
    await agentsStore.shutdownWorker(props.id)
    _injectChat(`[Foreman] Sent shutdown signal to ${props.id} (operator-initiated).`)
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
    await agentsStore.assignTask(props.id, {
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
  if (!taskId || !instructions || workerRedirecting.value) return
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  workerRedirecting.value = true
  workerRedirectError.value = ''
  try {
    await tasksStore.redirectTask(guildId, taskId, instructions)
    _injectChat(
      `[Foreman] Redirected task ${taskId} on ${_workerLabel()}: '${instructions.slice(0, 80)}'`,
    )
    redirectInstructions.value = ''
    redirectTaskId.value = ''
  } catch (e: unknown) {
    workerRedirectError.value = e instanceof Error ? e.message : String(e)
  } finally {
    workerRedirecting.value = false
  }
}

async function handleCancelTask() {
  const taskId = cancelTaskId.value
  if (!taskId || workerCancelling.value) return
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  workerCancelling.value = true
  workerCancelError.value = ''
  try {
    await tasksStore.cancelTask(guildId, taskId)
    _injectChat(`[Foreman] Cancelled task ${taskId} on ${_workerLabel()} (operator-initiated).`)
    cancelTaskId.value = ''
  } catch (e: unknown) {
    workerCancelError.value = e instanceof Error ? e.message : String(e)
  } finally {
    workerCancelling.value = false
  }
}

watch(
  () => props.id,
  () => {
    if (props.kind !== 'worker') return
    expanded.value = false
    actionError.value = ''
    followupTaskId.value = ''
    followupInstructions.value = ''
    redirectTaskId.value = ''
    redirectInstructions.value = ''
    cancelTaskId.value = ''
  },
)

/* ------------------------------------------------------------------ */
/* Task: redirect/cancel/followup panels                               */
/* ------------------------------------------------------------------ */

const showTaskPanel = computed(
  () => props.kind === 'task' || (props.kind === 'agent' && !!props.taskId),
)

const effectiveTaskId = computed(() => (props.kind === 'task' ? props.id : props.taskId) ?? '')
const taskState = computed(() => props.taskState)

const isWorkerTask = computed(() => props.taskWorkerId && props.taskWorkerId !== 'foreman')
const isInteractive = computed(() => props.taskType === 'interactive')

const TASK_TERMINAL_STATES = new Set(['done', 'failed', 'cancelled', 'error'])

const isTerminalState = computed(() => TASK_TERMINAL_STATES.has(props.taskState || ''))

const showFollowupPanel = computed(
  () =>
    !isInteractive.value &&
    isWorkerTask.value &&
    (props.taskState === 'awaiting-review' || isTerminalState.value),
)
const followupPanelHeader = computed(() =>
  isTerminalState.value ? 'SEND TO FOREMAN' : 'FOREMAN FOLLOW-UP',
)

const followupPlaceholder = computed(() =>
  isTerminalState.value
    ? 'Request additional work on this branch — foreman will re-assign it…'
    : "Additional instructions (e.g. 'update tests', 'add docstrings')…",
)

const canCancel = computed(() => {
  const s = props.taskState
  return isWorkerTask.value && (s === 'pending' || s === 'working' || s === 'awaiting-review')
})

const followupText = ref('')
const redirectText = ref('')
const taskCancelling = ref(false)
const taskCancelError = ref('')
const taskRedirecting = ref(false)
const messaging = ref(false)
const messageText = ref('')

async function sendFollowupAction() {
  const text = followupText.value.trim()
  if (!text) return
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  try {
    await tasksStore.sendFollowup(guildId, effectiveTaskId.value, text)
    followupText.value = ''
  } catch (e) {
    console.error('Follow-up failed', e)
  }
}

async function finalizeTaskAction() {
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  try {
    await tasksStore.finalizeTask(guildId, effectiveTaskId.value)
  } catch (e) {
    console.error('Finalize failed', e)
  }
}

async function cancelTaskAction() {
  const guildId = guildStore.currentGuild?.id
  if (!guildId || taskCancelling.value) return
  taskCancelling.value = true
  taskCancelError.value = ''
  try {
    await tasksStore.cancelTask(guildId, effectiveTaskId.value)
  } catch (e) {
    taskCancelError.value = e instanceof Error ? e.message : 'Cancellation failed'
    console.error('Cancel failed', e)
  } finally {
    taskCancelling.value = false
  }
}

async function sendTaskMessage() {
  const text = messageText.value.trim()
  if (!text || messaging.value) return
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  messaging.value = true
  try {
    await tasksStore.messageTask(guildId, effectiveTaskId.value, text)
    messageText.value = ''
  } catch (e) {
    console.error('Message failed', e)
  } finally {
    messaging.value = false
  }
}

async function sendRedirect() {
  const text = redirectText.value.trim()
  if (!text || taskRedirecting.value) return
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  taskRedirecting.value = true
  try {
    await tasksStore.redirectTask(guildId, effectiveTaskId.value, text)
    redirectText.value = ''
  } catch (e) {
    console.error('Redirect failed', e)
  } finally {
    taskRedirecting.value = false
  }
}
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

.actions-header {
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 2px;
  color: var(--color-amber);
}

.run-bar {
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

.action-error {
  padding: 4px 0 0;
  font-size: 11px;
  color: var(--color-red);
}

.action-row {
  display: flex;
  align-items: center;
  gap: 6px;
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

.worker-followup-btn {
  color: var(--color-amber);
  border-color: var(--color-amber);
  font-size: 9px;
  padding: 4px 10px;
  align-self: flex-start;
}
.worker-followup-btn:not(:disabled):hover {
  background: rgba(255, 204, 0, 0.12);
  box-shadow: 0 0 8px rgba(255, 204, 0, 0.3);
}
.worker-followup-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}

.worker-redirect-btn {
  color: var(--color-blue);
  border-color: var(--color-blue);
  font-size: 9px;
  padding: 4px 10px;
  align-self: flex-start;
}
.worker-redirect-btn:not(:disabled):hover {
  background: rgba(80, 120, 255, 0.12);
  box-shadow: 0 0 8px rgba(80, 120, 255, 0.3);
}
.worker-redirect-btn:disabled {
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
  align-items: center;
  gap: 8px;
  justify-content: flex-end;
}

.cancel-error {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--color-red);
  flex: 1;
}

.redirect-cancel-error {
  margin-top: 4px;
  padding: 0 2px;
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
