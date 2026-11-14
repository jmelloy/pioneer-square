<template>
  <div class="conversation-detail">
    <div v-if="!conversation" class="empty-state">Loading conversation…</div>
    <template v-else>
      <div class="pane-header">
        <div class="pane-title">
          <span class="title-icon">💬</span>
          <span class="title-text">{{ conversation.name || conversation.id }}</span>
          <code class="entity-id-chip">{{ conversation.id }}</code>
        </div>
        <div class="pane-meta">
          <span class="status-pill" :class="'status-' + conversation.status">
            {{ conversationsStore.statusLabel(conversation.status) }}
          </span>
          <button
            v-if="conversation.status !== 'closed'"
            class="pixel-btn close-btn"
            :disabled="acting"
            @click="onClose"
          >
            Close
          </button>
        </div>
      </div>

      <div class="pane-subheader">
        <span v-if="conversation.discord_thread_id" class="sub-field discord-chip">
          discord thread: {{ conversation.discord_thread_id }}
        </span>
        <span v-else class="sub-field discord-pending"> discord thread not yet created </span>
        <span class="sub-time-group">
          <span class="sub-time">created {{ formatRelative(conversation.created_at) }}</span>
          <span class="sub-time">updated {{ formatRelative(conversation.updated_at) }}</span>
        </span>
      </div>

      <div v-if="linkedTasks.length" class="pane-tasks">
        <div v-for="task in linkedTasks" :key="task.id" class="linked-task">
          <span class="linked-task-label">TASK</span>
          <a
            href="#"
            class="linked-task-name"
            :title="'Open task ' + task.id"
            @click.prevent="openTask(task.id)"
            >{{ task.name || task.id }}</a
          >
          <span
            class="linked-task-state"
            :class="'task-color-' + tasksStore.stateColor(task.state)"
            >{{ tasksStore.stateLabel(task.state) }}</span
          >
          <code v-if="task.branch" class="linked-task-branch">{{ task.branch }}</code>
          <a
            v-if="task.pr_url"
            :href="task.pr_url"
            target="_blank"
            rel="noopener noreferrer"
            class="linked-task-pr"
            >PR ↗</a
          >
        </div>
      </div>
      <div v-else-if="tasksError" class="pane-tasks pane-tasks-error">Task details unavailable</div>

      <div class="body chat-messages" ref="messagesEl">
        <button v-if="hasOlder" class="pixel-btn" :disabled="loadingOlder" @click="loadOlder">
          {{ loadingOlder ? 'Loading…' : 'Load earlier activity' }}
        </button>
        <div v-if="loadingMessages" class="chat-empty">Loading messages…</div>
        <div v-else-if="groupedMessages.length === 0" class="chat-empty">
          No messages in this conversation yet.
        </div>
        <div
          v-for="(msg, i) in groupedMessages"
          :key="i"
          class="chat-message"
          :class="[
            isToolUseGroup(msg) ? 'from-foreman msg-tool' : messageClasses(msg as ChatMessage),
            { 'worker-continuation': isWorkerContinuation(msg, i) },
          ]"
        >
          <div v-if="!isWorkerContinuation(msg, i)" class="msg-header">
            <span
              class="msg-from"
              :class="
                'msg-from--' + (isToolUseGroup(msg) ? msg.from : msgSender(msg as ChatMessage))
              "
              >{{ isToolUseGroup(msg) ? '⚙ FOREMAN' : senderLabel(msg as ChatMessage) }}</span
            >
            <button
              v-if="taskBadge(msg)"
              class="msg-task-badge"
              :title="'Open task ' + taskBadge(msg)"
              @click="openTask(taskBadge(msg)!)"
            >
              ⛬ {{ taskBadge(msg) }}
            </button>
            <span v-if="!isToolUseGroup(msg) && sourceLabel(msg as ChatMessage)" class="msg-source">
              via {{ sourceLabel(msg as ChatMessage) }}
            </span>
            <span v-if="(msg as ChatMessage).source !== 'worker'" class="msg-time">{{
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

          <LogLine
            v-else-if="(msg as ChatMessage).source === 'worker'"
            :log="workerLog(msg as ChatMessage)"
            :expanded="expandedWorkerMessage === _msgKey(msg as ChatMessage)"
            @toggle="toggleWorkerMessage(msg as ChatMessage)"
          />

          <template v-else>
            <span
              v-if="
                msgSender(msg as ChatMessage) !== 'user' &&
                msgSender(msg as ChatMessage) !== 'system'
              "
              class="msg-content msg-content--markdown"
              v-artifacts="guildStore.currentGuild?.id"
              v-html="renderMarkdown((msg as ChatMessage).content)"
            ></span>
            <span v-else class="msg-content">{{ (msg as ChatMessage).content }}</span>
          </template>
        </div>
      </div>

      <div v-if="conversation.status !== 'closed'" class="reply-row">
        <AttachmentTray
          :attachments="attach.attachments.value"
          :notice="attach.notice.value"
          @remove="attach.remove"
          @retry="attach.retry"
        />
        <div
          class="reply-input-group"
          :class="{ dragging: attach.dragging.value }"
          @dragover="attach.onDragOver"
          @dragleave="attach.onDragLeave"
          @drop="attach.onDrop"
        >
          <AttachButton @files="attach.addFiles" />
          <textarea
            v-model="replyText"
            class="reply-input"
            placeholder="Reply..."
            rows="3"
            :disabled="sending"
            @keydown.enter.exact.prevent="onReply"
            @paste="attach.onPaste"
          ></textarea>
          <button
            class="pixel-btn send-btn"
            :disabled="
              sending || attach.uploading.value || (!replyText.trim() && !attach.ready.value.length)
            "
            @click="onReply"
          >
            {{ sending ? '…' : '▶' }}
          </button>
        </div>
        <div v-if="replyError" class="reply-error">{{ replyError }}</div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import AttachmentTray from './AttachmentTray.vue'
import AttachButton from './AttachButton.vue'
import { useAttachments } from '../composables/useAttachments'
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useGuildStore } from '../stores/guild'
import { useConversationsStore } from '../stores/conversations'
import { useTasksStore } from '../stores/tasks'
import { useUiStore } from '../stores/ui'
import { formatClock, formatRelative } from '../utils/format'
import { renderMarkdown } from '../utils/markdown'
import { vArtifacts } from '../utils/artifacts'
import { ApiError } from '../utils/api'
import { useChatGrouping, isToolUseGroup } from '../composables/useChatGrouping'
import type { GroupedMessage } from '../composables/useChatGrouping'
import LogLine from './log-pane/LogLine.vue'
import type { ChatMessage, LogEntry } from '../types'

const props = defineProps<{
  id: number
}>()

const guildStore = useGuildStore()
const conversationsStore = useConversationsStore()
const tasksStore = useTasksStore()
const uiStore = useUiStore()

const acting = ref(false)
const loadingMessages = ref(false)
const messagesEl = ref<HTMLElement | null>(null)
const replyText = ref('')
const attach = useAttachments(() => guildStore.currentGuild?.id)
const sending = ref(false)
const replyError = ref<string | null>(null)

const conversation = computed(() => conversationsStore.conversations.find((c) => c.id === props.id))

const tasksError = ref(false)

// Tasks spawned from / linked to this conversation (tasks.conversation_id).
const linkedTasks = computed(() => tasksStore.tasks.filter((t) => t.conversation_id === props.id))

function openTask(taskId: string) {
  uiStore.selectTask(taskId)
  uiStore.openTaskTab(taskId)
}

// History fetched once per conversation (#1298) — separate from the live WS
// feed below since the guild-wide `guildStore.messages` window is capped and
// may have already scrolled this conversation's older messages out.
const history = ref<ChatMessage[]>([])
const hasOlder = ref(false)
const oldestId = ref<number>()
const loadingOlder = ref(false)

async function loadOlder() {
  const guildId = guildStore.currentGuild?.id
  if (!guildId || oldestId.value == null || loadingOlder.value) return
  const conversationId = props.id
  loadingOlder.value = true
  const el = messagesEl.value
  const height = el?.scrollHeight || 0
  try {
    const page = await conversationsStore.fetchConversationMessages(
      guildId,
      conversationId,
      oldestId.value,
    )
    if (props.id !== conversationId) return
    history.value = [...page, ...history.value]
    hasOlder.value = page.length === 100
    oldestId.value = typeof page[0]?.id === 'number' ? page[0].id : undefined
    await nextTick()
    if (el) el.scrollTop += el.scrollHeight - height
  } finally {
    loadingOlder.value = false
  }
}

// Live messages arrive on the shared guild WS connection (see stores/guild.ts)
// tagged with `conversationId` (backend/foreman/journal.py, routes/conversations.py);
// filter down to this conversation so in-progress/streaming replies show up
// immediately.
const liveMessages = computed(() =>
  guildStore.messages.filter((m) => (m as ChatMessage).conversationId === props.id),
)

function _msgKey(m: ChatMessage): string {
  const timestamp = (m.createdAt || m.created_at || '').replace(/\+00:00$/, 'Z')
  return `${timestamp}|${m.from || m.from_agent}|${m.taskId || ''}|${m.content}`
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
  return merged.sort(
    (a, b) =>
      Date.parse(a.createdAt || a.created_at || '') - Date.parse(b.createdAt || b.created_at || ''),
  )
})

const groupedMessages = useChatGrouping(messages)
const expandedWorkerMessage = ref<string | null>(null)

function workerLog(msg: ChatMessage): LogEntry {
  return {
    line: msg.content,
    timestamp: msg.createdAt || msg.created_at || '',
    detail: msg.detail,
    level: msg.level,
  }
}

function toggleWorkerMessage(msg: ChatMessage) {
  const key = _msgKey(msg)
  expandedWorkerMessage.value = expandedWorkerMessage.value === key ? null : key
}

function isWorkerContinuation(msg: GroupedMessage, index: number): boolean {
  const previous = groupedMessages.value[index - 1]
  if (isToolUseGroup(msg) || !previous || isToolUseGroup(previous)) return false
  return (
    msg.source === 'worker' &&
    previous.source === 'worker' &&
    msgSender(msg) === msgSender(previous) &&
    msg.taskId === previous.taskId
  )
}

function msgSender(msg: ChatMessage): string {
  return (msg.from || msg.from_agent || 'unknown') as string
}

function messageClasses(msg: ChatMessage) {
  const sender = msgSender(msg)
  return {
    'from-user': sender === 'user',
    'from-system': sender === 'system',
    'from-worker': msg.source === 'worker',
    'from-github': sender === 'github',
    'from-foreman': sender === 'foreman',
    'from-agent': sender !== 'user' && sender !== 'system',
  }
}

function senderLabel(msg: ChatMessage): string {
  const sender = msgSender(msg)
  if (sender === 'user') return 'YOU'
  if (sender === 'system') return 'SYS'
  if (sender === 'foreman') return '⚙ FOREMAN'
  if (msg.source === 'worker') return `WORKER · ${sender}`
  return sender.toUpperCase()
}

const SOURCE_LABELS: Record<string, string> = {
  discord: 'Discord',
  api: 'API',
  web: 'Web',
  worker: 'Worker',
  github: 'GitHub',
}

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
  const conversationId = props.id
  history.value = []
  hasOlder.value = false
  oldestId.value = undefined
  if (!conversation.value) {
    try {
      await conversationsStore.fetchConversation(guildId, props.id)
    } catch (e) {
      console.error('Failed to fetch conversation', e)
    }
  }
  loadingMessages.value = true
  try {
    const page = await conversationsStore.fetchConversationMessages(guildId, conversationId)
    if (props.id !== conversationId) return
    history.value = page
    hasOlder.value = page.length === 100
    oldestId.value = typeof page[0]?.id === 'number' ? page[0].id : undefined
  } finally {
    loadingMessages.value = false
  }
  if (!tasksStore.tasks.length) {
    tasksError.value = !(await tasksStore.fetchTasks(guildId))
  }
}

async function onReply() {
  const guildId = guildStore.currentGuild?.id
  const content = attach.compose(replyText.value)
  if (!guildId || !content || sending.value || attach.uploading.value || attach.hasErrors.value)
    return
  sending.value = true
  replyError.value = null
  try {
    // Append immediately; the server's WS broadcast (deduped via _msgKey)
    // will land on top of this once it arrives.
    const posted = await conversationsStore.postConversationMessage(guildId, props.id, content)
    history.value = [...history.value, posted]
    replyText.value = ''
    attach.clear()
  } catch (e) {
    replyError.value = e instanceof ApiError ? e.message : 'Failed to send reply'
  } finally {
    sending.value = false
  }
}

async function onClose() {
  const guildId = guildStore.currentGuild?.id
  if (!guildId || acting.value) return
  acting.value = true
  try {
    await conversationsStore.closeConversation(guildId, props.id)
  } finally {
    acting.value = false
  }
}

onMounted(load)

watch(() => props.id, load)

// Distance (px) from the bottom still counted as "at the bottom".
const BOTTOM_THRESHOLD = 48

function isNearBottom(): boolean {
  const el = messagesEl.value
  if (!el) return true
  return el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_THRESHOLD
}

watch(
  messages,
  async () => {
    // Capture position before the DOM updates so we only follow new content
    // when the user was already at/near the bottom.
    const stick = !loadingOlder.value && isNearBottom()
    await nextTick()
    if (stick && messagesEl.value) {
      messagesEl.value.scrollTop = messagesEl.value.scrollHeight
    }
  },
  { deep: true },
)
</script>

<style scoped>
.conversation-detail {
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

.pane-tasks {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 6px 16px;
  background: var(--color-bg-tertiary);
  border-bottom: 1px solid var(--color-brass-dark);
  flex-shrink: 0;
  font-size: 11px;
}

.pane-tasks-error {
  color: var(--color-text-dim);
  font-style: italic;
}

.linked-task {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.linked-task-label {
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 1px;
  color: var(--color-amber);
}

.linked-task-name {
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

.linked-task-state {
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 1px;
  text-transform: uppercase;
  padding: 2px 6px;
  border-radius: 2px;
  background: rgba(255, 255, 255, 0.08);
  color: var(--color-text-dim);
  flex-shrink: 0;
}
.task-color-green {
  color: var(--color-green);
}
.task-color-teal {
  color: var(--color-teal);
}
.task-color-amber {
  color: var(--color-amber);
}
.task-color-red {
  color: var(--color-red);
}
.task-color-blue {
  color: var(--color-blue, var(--color-teal));
}
.task-color-orange {
  color: var(--color-orange, var(--color-amber));
}

.linked-task-branch {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--color-text-dim);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

.linked-task-pr {
  color: var(--color-teal);
  font-size: 10px;
  flex-shrink: 0;
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

.chat-message.from-worker {
  align-self: stretch;
  max-width: 100%;
  border: none;
  border-left: 2px solid var(--color-teal);
  border-radius: 0;
  background: none;
  padding: 2px 10px;
}

.worker-continuation {
  margin-top: -6px;
}

.chat-message.from-github {
  border-left-color: var(--color-blue);
  border-top-color: rgba(68, 170, 238, 0.2);
  border-right-color: rgba(68, 170, 238, 0.2);
  border-bottom-color: rgba(68, 170, 238, 0.2);
  background: rgba(68, 170, 238, 0.06);
}

.msg-from--github {
  color: var(--color-blue);
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
  cursor: pointer;
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

.reply-row {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 12px;
  border-top: 2px solid var(--color-brass-dark);
  flex-shrink: 0;
}

.reply-input-group.dragging {
  outline: 2px dashed var(--color-brass);
  outline-offset: 2px;
}

.reply-input-group {
  display: flex;
  gap: 8px;
}

.reply-input {
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

.reply-input:disabled {
  opacity: 0.6;
}

@media (max-width: 768px) {
  .reply-input {
    font-size: 16px;
  }
}

.reply-input:focus {
  border-color: var(--color-brass);
  box-shadow: 0 0 8px rgba(232, 170, 0, 0.35);
}

.reply-input::placeholder {
  color: var(--color-text-dim);
  font-style: italic;
}

.reply-error {
  font-size: 10px;
  color: var(--color-red);
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
