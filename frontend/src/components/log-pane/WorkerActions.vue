<template>
  <div class="actions-panel">
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
        :disabled="workerState === 'offline' || shuttingDown"
        @click="handleShutdown"
        title="Ask the worker to shut down"
      >
        {{ shuttingDown ? '…' : '⏻ Shut down' }}
      </button>
    </div>
    <div v-if="actionError" class="action-error">{{ actionError }}</div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch } from 'vue'
import { useAgentsStore } from '../../stores/agents'
import { useGuildStore } from '../../stores/guild'
import { api } from '../../utils/api'
import type { WSInbound } from '../../types'

const props = defineProps<{ workerId: string; workerState?: string }>()

const agentsStore = useAgentsStore()
const guildStore = useGuildStore()

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
    await agentsStore.messageWorker(props.workerId, msg)
    messageInput.value = ''
  } catch (e: unknown) {
    actionError.value = e instanceof Error ? e.message : String(e)
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
      props.workerId,
      'Please shut down cleanly: finish any in-flight work and exit the worker process.',
    )
  } catch (e: unknown) {
    actionError.value = e instanceof Error ? e.message : String(e)
  } finally {
    shuttingDown.value = false
  }
}

async function fetchPendingAuth() {
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  try {
    const items = await api<Array<{ workerId: string; url: string }>>(
      `/guilds/${guildId}/pending-auth`,
    )
    const match = items.find((i) => i.workerId === props.workerId)
    pendingAuthUrl.value = match?.url ?? null
  } catch {
    // non-fatal
  }
}

function handleAuthEvent(data: WSInbound) {
  if (data.type === 'claude-auth-required' && data.workerId === props.workerId) {
    pendingAuthUrl.value = data.url
  } else if (
    (data.type === 'claude-auth-success' || data.type === 'claude-auth-cleared') &&
    data.workerId === props.workerId
  ) {
    pendingAuthUrl.value = null
  }
}

onMounted(async () => {
  await fetchPendingAuth()
  guildStore.addMessageHandler(handleAuthEvent)
})

onUnmounted(() => {
  guildStore.removeMessageHandler(handleAuthEvent)
})

watch(
  () => props.workerId,
  async () => {
    pendingAuthUrl.value = null
    await fetchPendingAuth()
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

.actions-header {
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 2px;
  color: var(--color-teal);
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
</style>
