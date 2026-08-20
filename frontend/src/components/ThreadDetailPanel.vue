<template>
  <div class="thread-detail">
    <div v-if="!thread" class="empty-state">Loading conversation…</div>
    <template v-else>
      <div class="pane-header">
        <div class="pane-title">
          <span class="title-icon">💬</span>
          <span class="title-text">{{ thread.name || thread.id }}</span>
          <code class="entity-id-chip">{{ thread.id }}</code>
        </div>
        <div class="pane-meta">
          <span class="status-pill" :class="'status-' + thread.status">
            {{ threadsStore.statusLabel(thread.status) }}
          </span>
        </div>
      </div>

      <div class="pane-subheader">
        <span class="sub-field">conversation #{{ thread.conversation_id }}</span>
        <span v-if="thread.discord_thread_id" class="sub-field discord-chip">
          discord thread: {{ thread.discord_thread_id }}
        </span>
        <span v-else class="sub-field discord-pending"> discord thread not yet created </span>
        <span class="sub-time-group">
          <span class="sub-time">created {{ formatRelative(thread.created_at) }}</span>
          <span class="sub-time">updated {{ formatRelative(thread.updated_at) }}</span>
        </span>
      </div>

      <div class="body chat-messages" ref="messagesEl">
        <div v-if="loadingMessages" class="chat-empty">Loading messages…</div>
        <div v-else-if="groupedMessages.length === 0" class="chat-empty">
          No messages in this conversation yet.
        </div>
        <div
          v-for="(msg, i) in groupedMessages"
          :key="i"
          class="chat-message"
          :class="
            isToolUseGroup(msg) ? 'from-foreman msg-tool' : messageClasses(msg as ChatMessage)
          "
        >
          <div class="msg-header">
            <span
              class="msg-from"
              :class="
                'msg-from--' + (isToolUseGroup(msg) ? msg.from : msgSender(msg as ChatMessage))
              "
              >{{ isToolUseGroup(msg) ? '⚙ FOREMAN' : senderLabel(msg as ChatMessage) }}</span
            >
            <span
              v-if="taskBadge(msg)"
              class="msg-task-badge"
              :title="'References task ' + taskBadge(msg)"
            >
              ⛬ {{ taskBadge(msg) }}
            </span>
            <span v-if="!isToolUseGroup(msg) && sourceLabel(msg as ChatMessage)" class="msg-source">
              via {{ sourceLabel(msg as ChatMessage) }}
            </span>
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
                msgSender(msg as ChatMessage) !== 'user' &&
                msgSender(msg as ChatMessage) !== 'system'
              "
              class="msg-content msg-content--markdown"
              v-html="renderMarkdown((msg as ChatMessage).content)"
            ></span>
            <span v-else class="msg-content">{{ (msg as ChatMessage).content }}</span>
          </template>
        </div>
      </div>

      <div class="actions">
        <button
          v-if="thread.status === 'active'"
          class="pixel-btn"
          :disabled="acting"
          @click="onArchive"
        >
          Archive
        </button>
        <button
          v-if="thread.status !== 'closed'"
          class="pixel-btn close-btn"
          :disabled="acting"
          @click="onClose"
        >
          Close
        </button>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useGuildStore } from '../stores/guild'
import { useThreadsStore } from '../stores/threads'
import { formatClock, formatRelative } from '../utils/format'
import { renderMarkdown } from '../utils/markdown'
import { useChatGrouping, isToolUseGroup } from '../composables/useChatGrouping'
import type { GroupedMessage } from '../composables/useChatGrouping'
import type { ChatMessage } from '../types'

const props = defineProps<{
  id: string
}>()

const guildStore = useGuildStore()
const threadsStore = useThreadsStore()

const acting = ref(false)
const loadingMessages = ref(false)
const messagesEl = ref<HTMLElement | null>(null)

const thread = computed(() => threadsStore.threads.find((t) => t.id === props.id))

// History fetched once per thread (#1175) — separate from the live WS feed
// below since the guild-wide `guildStore.messages` window is capped and may
// have already scrolled this thread's older messages out.
const history = ref<ChatMessage[]>([])

// Live messages arrive on the shared guild WS connection (see stores/guild.ts)
// tagged with `threadId` (backend/foreman/runner.py, ws_handlers.py); filter
// down to this thread so in-progress/streaming replies show up immediately.
const liveMessages = computed(() =>
  guildStore.messages.filter((m) => (m as ChatMessage).threadId === props.id),
)

function _msgKey(m: ChatMessage): string {
  return `${m.createdAt || m.created_at}|${m.from || m.from_agent}|${m.content}`
}

// Merge fetched history with the live feed, deduping messages the live feed
// already delivered before/after the REST fetch landed.
const messages = computed<ChatMessage[]>(() => {
  const seen = new Set(history.value.map(_msgKey))
  const merged = [...history.value]
  for (const m of liveMessages.value) {
    const key = _msgKey(m)
    if (!seen.has(key)) {
      seen.add(key)
      merged.push(m)
    }
  }
  return merged
})

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

const SOURCE_LABELS: Record<string, string> = { discord: 'Discord', api: 'API', web: 'Web' }

function sourceLabel(msg: ChatMessage): string | null {
  const source = msg.source
  if (!source || source === 'web') return null
  return SOURCE_LABELS[source] ?? source
}

const formatTime = (iso?: string) => formatClock(iso)

function taskBadge(msg: GroupedMessage): string | null {
  const taskId = (msg as { taskId?: string | null }).taskId
  return taskId ? taskId : null
}

async function load() {
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  if (!thread.value) {
    try {
      await threadsStore.fetchThread(guildId, props.id)
    } catch (e) {
      console.error('Failed to fetch thread', e)
    }
  }
  loadingMessages.value = true
  try {
    history.value = await threadsStore.fetchThreadMessages(guildId, props.id)
  } finally {
    loadingMessages.value = false
  }
}

async function onArchive() {
  const guildId = guildStore.currentGuild?.id
  if (!guildId || acting.value) return
  acting.value = true
  try {
    await threadsStore.archiveThread(guildId, props.id)
  } finally {
    acting.value = false
  }
}

async function onClose() {
  const guildId = guildStore.currentGuild?.id
  if (!guildId || acting.value) return
  acting.value = true
  try {
    await threadsStore.closeThread(guildId, props.id)
  } finally {
    acting.value = false
  }
}

onMounted(load)

watch(() => props.id, load)

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
.thread-detail {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: #080500;
}

.empty-state {
  padding: 24px;
  text-align: center;
  color: var(--color-text-dim);
  font-size: 12px;
  font-style: italic;
}

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

.title-icon {
  color: var(--color-brass);
}

.title-text {
  color: var(--color-text);
}

.entity-id-chip {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--color-text-dim);
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid var(--color-brass-dark);
  padding: 1px 5px;
  border-radius: 2px;
  letter-spacing: 0.5px;
}

.status-pill {
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 1px;
  text-transform: uppercase;
  padding: 2px 6px;
  border-radius: 2px;
}

.status-active {
  background: rgba(80, 200, 120, 0.2);
  color: var(--color-green);
}
.status-archived {
  background: rgba(232, 170, 0, 0.2);
  color: var(--color-amber);
}
.status-closed {
  background: rgba(255, 255, 255, 0.08);
  color: var(--color-text-dim);
}

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

.sub-field {
  font-size: 10px;
  color: var(--color-text-dim);
  font-family: var(--font-mono);
}

.discord-chip {
  color: var(--color-teal);
}

.discord-pending {
  font-style: italic;
}

.sub-time-group {
  display: flex;
  gap: 10px;
  margin-left: auto;
}

.sub-time {
  font-size: 10px;
  color: var(--color-text-dim);
}

.body.chat-messages {
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

.msg-source {
  font-size: 9px;
  color: var(--color-text-dim);
  font-style: italic;
  flex-shrink: 0;
}

.msg-task-badge {
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 0.5px;
  color: var(--color-amber);
  background: rgba(232, 170, 0, 0.12);
  border: 1px solid rgba(232, 170, 0, 0.4);
  border-radius: 2px;
  padding: 1px 4px;
  flex-shrink: 0;
  white-space: nowrap;
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

.actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 12px 16px;
  border-top: 2px solid var(--color-brass-dark);
  background: var(--color-bg-tertiary);
  flex-shrink: 0;
}

.close-btn {
  background: transparent;
  border-color: var(--color-red);
  color: var(--color-red);
}

.close-btn:hover {
  background: rgba(255, 80, 80, 0.12);
  box-shadow: none;
}
</style>
