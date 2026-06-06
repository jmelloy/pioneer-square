<template>
  <div class="chat-messages" ref="messagesEl">
    <div v-if="messages.length === 0" class="chat-empty">Awaiting foreman connection...</div>
    <div
      v-for="(msg, i) in groupedMessages"
      :key="i"
      class="chat-message"
      :class="isToolUseGroup(msg) ? 'from-foreman msg-tool' : messageClasses(msg as ChatMessage)"
    >
      <div class="msg-header">
        <span
          class="msg-from"
          :class="'msg-from--' + (isToolUseGroup(msg) ? msg.from : msgSender(msg as ChatMessage))"
          >{{ isToolUseGroup(msg) ? '⚙ FOREMAN' : senderLabel(msg as ChatMessage) }}</span
        >
        <span class="msg-time">{{
          formatTime(
            isToolUseGroup(msg)
              ? msg.createdAt || msg.created_at
              : (msg as ChatMessage).createdAt || (msg as ChatMessage).created_at,
          )
        }}</span>
      </div>

      <template v-if="isToolUseGroup(msg)">
        <span class="tool-use-summary">
          <span class="tool-use-label">{{ msg.tools.join(', ') }}</span>
          <span v-if="msg.pending" class="typing-indicator" aria-label="Tool call in progress">
            <span class="typing-dot"></span>
            <span class="typing-dot"></span>
            <span class="typing-dot"></span>
          </span>
          <span v-else class="tool-use-done" aria-label="Tool call complete">✓</span>
        </span>
      </template>

      <template v-else>
        <span
          v-if="
            msgSender(msg as ChatMessage) !== 'user' && msgSender(msg as ChatMessage) !== 'system'
          "
          class="msg-content msg-content--markdown"
          v-html="renderMarkdown((msg as ChatMessage).content)"
        ></span>
        <span v-else class="msg-content">{{ (msg as ChatMessage).content }}</span>
        <a
          v-if="(msg as ChatMessage).prUrl"
          :href="(msg as ChatMessage).prUrl!"
          target="_blank"
          rel="noopener noreferrer"
          class="pr-link"
        >
          Open PR →
        </a>
      </template>
    </div>
  </div>
  <div class="chat-input-row">
    <textarea
      v-model="inputText"
      class="chat-input"
      placeholder="Send directive..."
      rows="3"
      @keydown.enter.exact.prevent="onSend"
    ></textarea>
    <button class="pixel-btn send-btn" @click="onSend">▶</button>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick } from 'vue'
import { renderMarkdown } from '../../utils/markdown'
import { useGuildStore } from '../../stores/guild'
import { formatClock } from '../../utils/format'
import { useChatGrouping, isToolUseGroup } from '../../composables/useChatGrouping'
import type { ChatMessage } from '../../types'

const guildStore = useGuildStore()

const inputText = ref('')
const messagesEl = ref<HTMLElement | null>(null)

const messages = computed(() => guildStore.messages)
const groupedMessages = useChatGrouping(messages)

function msgSender(msg: ChatMessage): string {
  return (msg.from || msg.from_agent || 'unknown') as string
}

function messageClasses(msg: ChatMessage) {
  const sender = msgSender(msg)
  return {
    'from-user': sender === 'user',
    'from-system': sender === 'system',
    'from-foreman': sender === 'foreman',
    'from-agent': sender !== 'user' && sender !== 'system',
  }
}

function senderLabel(msg: ChatMessage): string {
  const sender = msgSender(msg)
  if (sender === 'user') return 'YOU'
  if (sender === 'system') return 'SYS'
  if (sender === 'foreman') return '⚙ FOREMAN'
  return sender.toUpperCase()
}

const formatTime = (iso?: string) => formatClock(iso)

function onSend() {
  const text = inputText.value.trim()
  if (!text) return

  guildStore.sendMessage({
    type: 'chat',
    from: 'user',
    to: 'foreman',
    content: text,
  })
  inputText.value = ''
}

defineExpose({
  focusInput: () => {
    nextTick(() => {
      ;(document.querySelector('.chat-input') as HTMLInputElement | null)?.focus()
    })
  },
  setInput: (text: string) => {
    inputText.value = text
  },
})

watch(
  messages,
  async () => {
    await nextTick()
    if (messagesEl.value) {
      messagesEl.value.scrollTop = messagesEl.value.scrollHeight
    }
  },
  { deep: true },
)
</script>

<style scoped>
.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 0;
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
  gap: 4px;
  padding: 6px 10px;
  border-radius: 2px;
  max-width: 90%;
}

.chat-message.from-user {
  align-self: flex-end;
  background: rgba(232, 170, 0, 0.15);
  border: 1px solid var(--color-brass-dark);
  border-right: 3px solid var(--color-brass);
}

.chat-message.from-agent {
  align-self: flex-start;
  background: rgba(0, 187, 170, 0.08);
  border: 1px solid rgba(0, 187, 170, 0.25);
  border-left: 3px solid var(--color-teal);
}

.chat-message.from-foreman {
  align-self: flex-start;
  background: rgba(0, 187, 170, 0.1);
  border: 1px solid rgba(0, 187, 170, 0.35);
  border-left: 3px solid var(--color-teal);
  max-width: 95%;
}

.chat-message.msg-tool {
  align-self: flex-start;
  background: rgba(0, 0, 0, 0.25);
  border: 1px solid rgba(232, 170, 0, 0.15);
  border-left: 3px solid var(--color-brass-dark);
  max-width: 95%;
  padding: 5px 8px;
}

.chat-message.from-system {
  align-self: center;
  background: rgba(80, 80, 80, 0.08);
  border: 1px solid rgba(120, 120, 120, 0.2);
  max-width: 95%;
}

.msg-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.msg-from {
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 1px;
  color: var(--color-brass);
}

.msg-from--user {
  color: var(--color-brass-light);
}

.msg-from--foreman {
  color: var(--color-teal);
  background: rgba(0, 187, 170, 0.15);
  border: 1px solid rgba(0, 187, 170, 0.4);
  padding: 1px 5px;
  letter-spacing: 1.5px;
}

.msg-from--system {
  color: var(--color-text-dim);
  font-size: 6px;
}

.from-system .msg-content {
  font-size: 10px;
  color: var(--color-text-dim);
  font-style: italic;
}

.pr-link {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-teal);
  text-decoration: none;
  align-self: flex-end;
  margin-top: 2px;
  padding: 2px 6px;
  border: 1px solid var(--color-teal);
  transition: all 0.15s;
}

.pr-link:hover {
  background: rgba(0, 187, 170, 0.15);
  box-shadow: 0 0 6px rgba(0, 187, 170, 0.4);
}

.msg-content {
  font-size: 12px;
  color: var(--color-text);
  line-height: 1.4;
  word-break: break-word;
}

.msg-content--markdown :deep(p) {
  margin: 0 0 6px;
}
.msg-content--markdown :deep(p:last-child) {
  margin-bottom: 0;
}
.msg-content--markdown :deep(ul),
.msg-content--markdown :deep(ol) {
  margin: 4px 0 6px;
  padding-left: 18px;
}
.msg-content--markdown :deep(li) {
  margin-bottom: 2px;
}
.msg-content--markdown :deep(code) {
  font-family: var(--font-mono);
  font-size: 11px;
  background: rgba(0, 0, 0, 0.35);
  border: 1px solid var(--color-brass-dark);
  padding: 0 4px;
  border-radius: 2px;
  color: var(--color-amber);
}
.msg-content--markdown :deep(pre) {
  background: rgba(0, 0, 0, 0.4);
  border: 1px solid var(--color-brass-dark);
  padding: 8px 10px;
  overflow-x: auto;
  margin: 4px 0;
}
.msg-content--markdown :deep(pre code) {
  background: none;
  border: none;
  padding: 0;
  font-size: 10px;
  color: var(--color-green);
}
.msg-content--markdown :deep(h1),
.msg-content--markdown :deep(h2),
.msg-content--markdown :deep(h3) {
  font-family: var(--font-pixel);
  font-size: 8px;
  letter-spacing: 1px;
  color: var(--color-teal);
  margin: 6px 0 4px;
  text-transform: uppercase;
}
.msg-content--markdown :deep(strong) {
  color: var(--color-amber);
  font-weight: bold;
}
.msg-content--markdown :deep(em) {
  color: var(--color-text-dim);
  font-style: italic;
}
.msg-content--markdown :deep(a) {
  color: var(--color-teal);
  text-decoration: underline;
}
.msg-content--markdown :deep(blockquote) {
  border-left: 3px solid var(--color-brass-dark);
  margin: 4px 0;
  padding: 2px 8px;
  color: var(--color-text-dim);
  font-style: italic;
}

.msg-time {
  font-size: 9px;
  color: var(--color-text-dim);
  flex-shrink: 0;
  margin-left: auto;
}

.tool-use-summary {
  display: flex;
  align-items: center;
  gap: 8px;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--color-text-dim);
  opacity: 0.75;
  font-style: italic;
}

.tool-use-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

.tool-use-done {
  color: var(--color-teal);
  font-style: normal;
  flex-shrink: 0;
}

.typing-indicator {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  flex-shrink: 0;
}

.typing-dot {
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: var(--color-brass);
  animation: typing-bounce 1.2s infinite ease-in-out;
}

.typing-dot:nth-child(2) {
  animation-delay: 0.2s;
}

.typing-dot:nth-child(3) {
  animation-delay: 0.4s;
}

@keyframes typing-bounce {
  0%,
  60%,
  100% {
    opacity: 0.3;
    transform: translateY(0);
  }
  30% {
    opacity: 1;
    transform: translateY(-2px);
  }
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
  padding: 6px 10px;
  outline: none;
  resize: none;
  line-height: 1.4;
  transition: border-color 0.15s;
}

@media (max-width: 768px) {
  .chat-input {
    font-size: 16px;
  }
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
