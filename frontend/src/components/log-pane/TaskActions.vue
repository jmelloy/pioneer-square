<template>
  <!-- Task: redirect panel (active during work) -->
  <div v-if="taskState === 'working' && isWorkerTask" class="redirect-panel">
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
  <div v-if="canCancel && taskState !== 'working'" class="cancel-panel">
    <button class="pixel-btn cancel-task-btn" @click="cancelTaskAction" :disabled="cancelling">
      {{ cancelling ? '…' : '✕ Cancel Task' }}
    </button>
  </div>

  <!-- Task: follow-up panel — visible for awaiting-review and terminal states -->
  <div v-if="showFollowupPanel" class="followup-panel">
    <div class="followup-header">{{ followupPanelHeader }}</div>
    <div class="followup-row">
      <textarea
        v-model="followupText"
        class="followup-input"
        :placeholder="followupPlaceholder"
        rows="2"
        @keydown.ctrl.enter="sendFollowupAction"
        @input="followupError = ''"
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
    <div v-if="followupError" class="followup-error">{{ followupError }}</div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useGuildStore } from '../../stores/guild'
import { useTasksStore } from '../../stores/tasks'

const props = defineProps<{
  taskId: string
  taskState?: string
  workerId?: string
}>()

const guildStore = useGuildStore()
const tasksStore = useTasksStore()

const isWorkerTask = computed(() => props.workerId && props.workerId !== 'foreman')

const _TERMINAL_STATES = new Set(['done', 'failed', 'cancelled'])

const isTerminalState = computed(() => _TERMINAL_STATES.has(props.taskState || ''))

const showFollowupPanel = computed(
  () =>
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
const followupError = ref('')
const redirectText = ref('')
const cancelling = ref(false)
const redirecting = ref(false)

function sendFollowupAction() {
  const text = followupText.value.trim()
  if (!text) return
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  followupText.value = ''
  followupError.value = ''
  tasksStore.sendFollowup(guildId, props.taskId, text).catch((e) => {
    followupError.value = e instanceof Error ? e.message : 'Follow-up failed'
    console.error('Follow-up failed', e)
  })
}

async function finalizeTaskAction() {
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  try {
    await tasksStore.finalizeTask(guildId, props.taskId)
  } catch (e) {
    console.error('Finalize failed', e)
  }
}

async function cancelTaskAction() {
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
</script>

<style scoped>
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

.followup-error {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--color-red);
  margin-top: 6px;
}
</style>
