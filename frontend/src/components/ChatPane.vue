<template>
  <div class="chat-pane" :class="{ minimized }">
    <div class="chat-header" @click="toggleMinimize">
      <span class="chat-title">⚙ FOREMAN COMMS</span>
      <div class="header-controls">
        <span class="agent-status" v-if="foreman">
          <span class="status-dot" :class="foreman.state"></span>
          {{ foreman.state }}
        </span>
        <span class="minimize-btn">{{ minimized ? '▲' : '▼' }}</span>
      </div>
    </div>
    <div v-if="!minimized" class="chat-body">
      <div class="chat-messages" ref="messagesEl">
        <div v-if="messages.length === 0" class="chat-empty">
          Awaiting foreman connection...
        </div>
        <div
          v-for="(msg, i) in messages"
          :key="i"
          class="chat-message"
          :class="{ 'from-user': msg.from === 'user', 'from-agent': msg.from !== 'user' }"
        >
          <span class="msg-from">{{ msg.from === 'user' ? 'YOU' : msg.from }}</span>
          <span class="msg-content">{{ msg.content }}</span>
          <span class="msg-time">{{ formatTime(msg.createdAt || msg.created_at) }}</span>
        </div>
      </div>
      <div class="chat-input-row">
        <input
          v-model="inputText"
          class="chat-input"
          placeholder="Send directive..."
          @keydown.enter="sendMessage"
        />
        <button class="pixel-btn send-btn" @click="sendMessage">▶</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, nextTick, watch } from 'vue'
import { useSessionStore } from '../stores/session.js'
import { useAgentsStore } from '../stores/agents.js'

const sessionStore = useSessionStore()
const agentsStore = useAgentsStore()

const minimized = ref(false)
const inputText = ref('')
const messagesEl = ref(null)

const messages = computed(() => sessionStore.messages)
const foreman = computed(() => agentsStore.agents.find(a => a.type === 'foreman'))

function toggleMinimize() {
  minimized.value = !minimized.value
}

function sendMessage() {
  if (!inputText.value.trim()) return
  sessionStore.sendMessage({
    type: 'chat',
    from: 'user',
    to: 'foreman',
    content: inputText.value.trim()
  })
  inputText.value = ''
}

function formatTime(isoStr) {
  if (!isoStr) return ''
  return new Date(isoStr).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

watch(messages, async () => {
  await nextTick()
  if (messagesEl.value) {
    messagesEl.value.scrollTop = messagesEl.value.scrollHeight
  }
}, { deep: true })
</script>

<style scoped>
.chat-pane {
  position: fixed;
  bottom: 0;
  right: 0;
  width: 360px;
  max-height: 450px;
  background: var(--color-bg-secondary);
  border-top: 3px solid var(--color-brass);
  border-left: 3px solid var(--color-brass);
  border-top-left-radius: 4px;
  display: flex;
  flex-direction: column;
  z-index: 100;
  box-shadow: -4px -4px 20px rgba(232, 170, 0, 0.2);
}

.chat-pane.minimized {
  max-height: 44px;
}

.chat-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  background: var(--color-bg-tertiary);
  border-bottom: 2px solid var(--color-brass-dark);
  cursor: pointer;
  flex-shrink: 0;
}

.chat-header:hover {
  background: rgba(232, 170, 0, 0.08);
}

.chat-title {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-brass-light);
  letter-spacing: 2px;
  text-shadow: 0 0 6px rgba(255, 214, 68, 0.4);
}

.header-controls {
  display: flex;
  align-items: center;
  gap: 8px;
}

.agent-status {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 10px;
  color: var(--color-text-dim);
}

.status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  display: inline-block;
}

.status-dot.idle { background: var(--color-text-dim); }
.status-dot.thinking { background: var(--color-blue); }
.status-dot.working { background: var(--color-green); }
.status-dot.busy { background: var(--color-orange); }
.status-dot.error { background: var(--color-red); }

.minimize-btn {
  font-size: 10px;
  color: var(--color-brass);
}

.chat-body {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-height: 0;
}

.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 200px;
  max-height: 340px;
}

.chat-empty {
  color: var(--color-text-dim);
  font-size: 11px;
  text-align: center;
  margin-top: 20px;
  font-style: italic;
}

.chat-message {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px 10px;
  border-radius: 2px;
  max-width: 90%;
}

.chat-message.from-user {
  align-self: flex-end;
  background: rgba(232, 170, 0, 0.12);
  border: 1px solid var(--color-brass-dark);
  border-right: 3px solid var(--color-brass);
}

.chat-message.from-agent {
  align-self: flex-start;
  background: rgba(0, 187, 170, 0.08);
  border: 1px solid rgba(0, 187, 170, 0.3);
  border-left: 3px solid var(--color-teal);
}

.msg-from {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-brass);
  letter-spacing: 1px;
}

.from-agent .msg-from {
  color: var(--color-teal);
}

.msg-content {
  font-size: 12px;
  color: var(--color-text);
  line-height: 1.4;
  word-break: break-word;
}

.msg-time {
  font-size: 9px;
  color: var(--color-text-dim);
  align-self: flex-end;
}

.chat-input-row {
  display: flex;
  gap: 8px;
  padding: 10px 12px;
  border-top: 2px solid var(--color-brass-dark);
  flex-shrink: 0;
}

.chat-input {
  flex: 1;
  background: var(--color-bg);
  border: 2px solid var(--color-brass-dark);
  color: var(--color-text);
  font-family: var(--font-mono);
  font-size: 12px;
  padding: 6px 10px;
  outline: none;
  transition: border-color 0.15s;
}

.chat-input:focus {
  border-color: var(--color-brass);
  box-shadow: 0 0 8px rgba(232, 170, 0, 0.35);
}

.chat-input::placeholder {
  color: var(--color-text-dim);
  font-style: italic;
}

.send-btn {
  padding: 6px 10px;
  font-size: 10px;
}
</style>
