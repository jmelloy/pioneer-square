<template>
  <div class="chat-pane panel-bg" :class="{ minimized }">
    <div class="chat-header" @click="toggleMinimize">
      <span class="chat-title">⚙ FOREMAN COMMS</span>
      <div class="header-controls">
        <span class="agent-status" v-if="foreman">
          <span class="status-dot" :class="foreman.state"></span>
          {{ foreman.state }}
        </span>
        <span v-if="pollLabel" class="poll-indicator">⏱ {{ pollLabel }}</span>
        <span class="minimize-btn">{{ minimized ? '▲' : '▼' }}</span>
      </div>
    </div>

    <div v-if="!minimized" class="chat-body">
      <!-- Tab bar — only show Issues tab when GitHub is configured -->
      <div class="tab-bar">
        <button
          class="tab-btn"
          :class="{ active: activeTab === 'chat' }"
          @click.stop="activeTab = 'chat'"
        >Chat</button>
        <button
          v-if="ghStore.isConfigured"
          class="tab-btn"
          :class="{ active: activeTab === 'issues' }"
          @click.stop="switchToIssues"
        >
          Issues
          <span v-if="ghStore.issues.length" class="badge">{{ ghStore.issues.length }}</span>
        </button>
      </div>

      <!-- Chat tab -->
      <template v-if="activeTab === 'chat'">
        <div class="chat-messages" ref="messagesEl">
          <div v-if="messages.length === 0" class="chat-empty">
            Awaiting foreman connection...
          </div>
          <div
            v-for="(msg, i) in messages"
            :key="i"
            class="chat-message"
            :class="messageClasses(msg)"
          >
            <div class="msg-header">
              <span class="msg-from" :class="'msg-from--' + msgSender(msg)">{{ senderLabel(msg) }}</span>
              <span class="msg-time">{{ formatTime(msg.createdAt || msg.created_at) }}</span>
            </div>

            <!-- Tool-use block -->
            <template v-if="isToolUse(msg)">
              <div class="tool-block tool-block--use">
                <button class="tool-block-header" @click.stop="toggleTool(i)">
                  <span class="tool-name">{{ expandedTool === i ? '▼' : '▶' }} tool_use{{ msg.toolName ? ' · ' + msg.toolName : '' }}</span>
                  <span v-if="expandedTool !== i" class="tool-preview">{{ toolPreview(msg.toolInput ? JSON.stringify(msg.toolInput) : msg.content) }}</span>
                </button>
                <div v-if="expandedTool === i" class="tool-body">
                  <pre class="tool-json">{{ JSON.stringify(msg.toolInput, null, 2) }}</pre>
                </div>
              </div>
            </template>

            <!-- Tool-result block -->
            <template v-else-if="isToolResult(msg)">
              <div class="tool-block tool-block--result" :class="{ 'tool-block--error': msg.isError }">
                <button class="tool-block-header" @click.stop="toggleTool(i)">
                  <span class="tool-name">{{ expandedTool === i ? '▼' : '▶' }}{{ msg.isError ? ' ✗' : '' }} tool_result{{ msg.toolName ? ' · ' + msg.toolName : '' }}</span>
                  <span v-if="expandedTool !== i" class="tool-preview">{{ toolPreview(msg.toolOutput || msg.content) }}</span>
                </button>
                <div v-if="expandedTool === i" class="tool-body">
                  <pre class="tool-output">{{ msg.toolOutput || msg.content }}</pre>
                </div>
              </div>
            </template>

            <!-- Regular message content -->
            <template v-else>
              <span
                v-if="msgSender(msg) !== 'user' && msgSender(msg) !== 'system'"
                class="msg-content msg-content--markdown"
                v-html="renderMarkdown(msg.content)"
              ></span>
              <span v-else class="msg-content">{{ msg.content }}</span>
              <a v-if="msg.prUrl" :href="msg.prUrl" target="_blank" rel="noopener" class="pr-link">
                Open PR →
              </a>
            </template>
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
      </template>

      <!-- Issues tab -->
      <template v-else-if="activeTab === 'issues'">
        <div class="issues-toolbar">
          <button class="pixel-btn refresh-btn" @click="() => refreshIssues()" :disabled="ghStore.loading">
            {{ ghStore.loading ? '...' : '↻' }}
          </button>
          <span class="issues-count">{{ ghStore.issues.length }} open</span>
          <span class="issues-repos">{{ guildStore.currentGuild?.primary_repo ?? (ghStore.selectedRepos.length + ' repo' + (ghStore.selectedRepos.length !== 1 ? 's' : '')) }}</span>
        </div>

        <div class="issues-list" ref="issuesEl">
          <div v-if="ghStore.issues.length === 0 && !ghStore.loading" class="chat-empty">
            No open issues found.
          </div>
          <div v-if="ghStore.loading" class="chat-empty">Loading issues...</div>

          <div
            v-for="issue in ghStore.issues"
            :key="issue.id"
            class="issue-row"
            @click="assignIssue(issue)"
            :title="'Click to assign this issue'"
          >
            <div class="issue-top">
              <span class="issue-repo">{{ issue.repo }}</span>
              <span class="issue-number">#{{ issue.number }}</span>
            </div>
            <div class="issue-title">{{ issue.title }}</div>
            <div class="issue-meta">
              <span
                v-for="label in issue.labels.slice(0, 3)"
                :key="label.id"
                class="issue-label"
                :style="{ borderColor: '#' + label.color, color: '#' + label.color }"
              >{{ label.name }}</span>
              <span class="issue-age">{{ formatAge(issue.created_at) }}</span>
            </div>
          </div>
        </div>
      </template>

    </div>
  </div>

  <!-- Claude auth modal — teleported to body so it overlays the entire UI -->
  <teleport to="body">
    <div v-if="claudeAuthPending" class="auth-modal-overlay">
      <div class="auth-modal">
        <div class="auth-modal-header">
          <span class="auth-modal-title">⚿ CLAUDE AUTH REQUIRED</span>
          <span class="auth-modal-worker">{{ claudeAuthPending.workerId }}</span>
        </div>
        <div class="auth-modal-body">
          <p class="auth-instruction">
            Visit the URL below to authenticate Claude, then paste the code here.
          </p>
          <a :href="claudeAuthPending.url" target="_blank" rel="noopener" class="auth-url-block">
            {{ claudeAuthPending.url }}
          </a>
          <div class="auth-input-row">
            <input
              v-model="authCodeInput"
              class="auth-input"
              placeholder="Paste auth code here..."
              @keydown.enter="submitAuthCode"
              autofocus
            />
            <button class="pixel-btn auth-submit-btn" @click="submitAuthCode" :disabled="!authCodeInput.trim()">
              ↵ Submit
            </button>
          </div>
        </div>
      </div>
    </div>
  </teleport>
</template>

<script setup lang="ts">
import { ref, computed, nextTick, watch, onMounted, onUnmounted } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { useGuildStore } from '../stores/guild'
import { useAgentsStore } from '../stores/agents'
import { useGitHubStore } from '../stores/github'
import { useAuthStore } from '../stores/auth'
import type { ChatMessage, GitHubIssue, WSMessage } from '../types'

const API_BASE = (import.meta.env.VITE_API_BASE as string) ?? ''

const guildStore = useGuildStore()
const agentsStore = useAgentsStore()
const ghStore = useGitHubStore()
const authStore = useAuthStore()

const minimized = ref(false)
const inputText = ref('')
const authCodeInput = ref('')
const claudeAuthPending = ref<{ workerId: string; url: string } | null>(null)
const messagesEl = ref<HTMLElement | null>(null)
const issuesEl = ref<HTMLElement | null>(null)
const activeTab = ref<'chat' | 'issues'>('chat')

const ISSUE_REFRESH_MS = 3 * 60 * 1000
let issueRefreshInterval: ReturnType<typeof setInterval> | null = null

// Poll countdown: epoch ms when next foreman check fires, plus a tick counter
// to force the computed to re-evaluate every 30 s.
const nextPollAt = ref<number | null>(null)
const pollTick = ref(0)
let pollCountdownTimer: ReturnType<typeof setInterval> | null = null

const pollLabel = computed(() => {
  void pollTick.value  // subscribe so re-render fires on each tick
  if (nextPollAt.value === null) return ''
  const remaining = Math.max(0, Math.ceil((nextPollAt.value - Date.now()) / 1000))
  if (remaining <= 0) return 'checking...'
  const mins = Math.ceil(remaining / 60)
  return `next check in ${mins}m`
})

const messages = computed(() => guildStore.messages)
const foreman = computed(() => agentsStore.agents.find(a => a.type === 'foreman'))
const expandedTool = ref<number | null>(null)

function msgSender(msg: ChatMessage): string {
  return (msg.from || msg.from_agent || 'unknown') as string
}

function isToolUse(msg: ChatMessage): boolean {
  return msg.role === 'tool_use'
}

function isToolResult(msg: ChatMessage): boolean {
  return msg.role === 'tool_result'
}

function messageClasses(msg: ChatMessage) {
  const sender = msgSender(msg)
  return {
    'from-user': sender === 'user',
    'from-system': sender === 'system',
    'from-foreman': sender === 'foreman',
    'from-agent': sender !== 'user' && sender !== 'system',
    'msg-tool': isToolUse(msg) || isToolResult(msg),
  }
}

function senderLabel(msg: ChatMessage): string {
  const sender = msgSender(msg)
  if (sender === 'user') return 'YOU'
  if (sender === 'system') return 'SYS'
  if (sender === 'foreman') return '⚙ FOREMAN'
  return sender.toUpperCase()
}

function toggleTool(i: number) {
  expandedTool.value = expandedTool.value === i ? null : i
}

function toolPreview(text: string | undefined | null): string {
  if (!text) return ''
  const s = text.replace(/\s+/g, ' ').trim()
  return s.length > 60 ? s.slice(0, 60) + '…' : s
}

// Issue assignment pattern: "Work on issue #N in owner/repo: title"
const ISSUE_PATTERN = /Work on issue #(\d+) in ([^:]+): "(.+)"/

function renderMarkdown(text: string): string {
  const html = marked.parse(text, { async: false }) as string
  return DOMPurify.sanitize(html, { ADD_ATTR: ['target', 'rel'] })
}

function toggleMinimize() {
  minimized.value = !minimized.value
}

async function sendMessage() {
  const text = inputText.value.trim()
  if (!text) return

  guildStore.sendMessage({
    type: 'chat',
    from: 'user',
    to: 'foreman',
    content: text,
  })
  inputText.value = ''

  // Auto-assign to first idle worker if the message matches issue pattern
  const match = text.match(ISSUE_PATTERN)
  if (match) {
    const [, issueNum, repoName] = match
    const worker = agentsStore.firstIdleWorker()
    if (worker) {
      try {
        await agentsStore.assignTask(worker.id, {
          description: text,
          issueNumber: parseInt(issueNum, 10),
          issueRepo: repoName.trim(),
        })
        guildStore.messages.push({
          type: 'chat',
          from: 'system',
          to: 'user',
          content: `Task assigned to ${worker.name}`,
          createdAt: new Date().toISOString(),
        })
      } catch (e: any) {
        guildStore.messages.push({
          type: 'chat',
          from: 'system',
          to: 'user',
          content: `Could not assign task: ${e.message}`,
          createdAt: new Date().toISOString(),
        })
      }
    } else {
      guildStore.messages.push({
        type: 'chat',
        from: 'system',
        to: 'user',
        content: 'No idle worker available. Deploy a worker first.',
        createdAt: new Date().toISOString(),
      })
    }
  }
}

function submitAuthCode() {
  const pending = claudeAuthPending.value
  const code = authCodeInput.value.trim()
  if (!pending || !code) return
  const sent = guildStore.sendMessage({
    type: 'worker-auth-response',
    workerId: pending.workerId,
    code,
  })
  if (!sent) {
    guildStore.messages.push({
      type: 'chat', from: 'system', to: 'user',
      content: '⚠ Connection not ready — please wait a moment and try again.',
      createdAt: new Date().toISOString(),
    })
    return
  }
  claudeAuthPending.value = null
  authCodeInput.value = ''
}

// Listen for task lifecycle + escalation broadcasts
function handleTaskEvent(data: WSMessage) {
  if (data.type === 'task-complete') {
    guildStore.messages.push({
      type: 'chat', from: 'system', to: 'user',
      content: data.prUrl
        ? `✓ ${data.workerId} done — PR: ${data.prUrl}`
        : `✓ ${data.workerId} finished (no PR)`,
      prUrl: data.prUrl || null,
      createdAt: new Date().toISOString(),
    })
  } else if (data.type === 'needs-input') {
    guildStore.messages.push({
      type: 'chat', from: 'system', to: 'user',
      content: `⚠ ${data.workerId} needs attention on: "${data.description}"`,
      createdAt: new Date().toISOString(),
    })
  } else if (data.type === 'claude-auth-required') {
    claudeAuthPending.value = { workerId: data.workerId, url: data.url }
    guildStore.messages.push({
      type: 'chat', from: 'system', to: 'user',
      content: `⚿ ${data.workerId} needs Claude auth — visit the URL and paste the code below`,
      createdAt: new Date().toISOString(),
    })
  } else if (data.type === 'task-assigned') {
    guildStore.messages.push({
      type: 'chat', from: 'system', to: 'user',
      content: `→ ${data.workerId} assigned: ${data.description}`,
      createdAt: new Date().toISOString(),
    })
  } else if (data.type === 'foreman-poll-status') {
    const secs = typeof data.nextCheckIn === 'number' ? data.nextCheckIn : null
    nextPollAt.value = secs !== null ? Date.now() + secs * 1000 : null
  }
}

onMounted(async () => {
  guildStore.addMessageHandler(handleTaskEvent)
  // Restore the auth panel if a worker was already waiting when we connected
  // (handles page-refresh and late-join scenarios where the original
  // claude-auth-required broadcast was missed).
  const guildId = guildStore.currentGuild?.id
  if (guildId && !claudeAuthPending.value) {
    try {
      const res = await fetch(`${API_BASE}/guilds/${guildId}/pending-auth`, {
        headers: authStore.authHeaders()
      })
      if (res.ok) {
        const items: Array<{ workerId: string; url: string }> = await res.json()
        if (items.length > 0) {
          claudeAuthPending.value = { workerId: items[0].workerId, url: items[0].url }
        }
      }
    } catch (e) {
      console.warn('Could not fetch pending auth state', e)
    }
  }

  if (ghStore.isConfigured) {
    issueRefreshInterval = setInterval(() => refreshIssues(true), ISSUE_REFRESH_MS)
  }
  // Increment tick every 30 s to keep the poll countdown label current.
  pollCountdownTimer = setInterval(() => { pollTick.value++ }, 30_000)
})
onUnmounted(() => {
  guildStore.removeMessageHandler(handleTaskEvent)
  if (issueRefreshInterval !== null) {
    clearInterval(issueRefreshInterval)
    issueRefreshInterval = null
  }
  if (pollCountdownTimer !== null) {
    clearInterval(pollCountdownTimer)
    pollCountdownTimer = null
  }
})

function _primaryRepoList(): string[] | undefined {
  const repo = guildStore.currentGuild?.primary_repo
  return repo ? [repo] : undefined
}

async function switchToIssues() {
  activeTab.value = 'issues'
  if (ghStore.issues.length === 0 && !ghStore.loading) {
    await ghStore.fetchIssues(_primaryRepoList())
  }
}

async function refreshIssues(silent = false) {
  await ghStore.fetchIssues(_primaryRepoList(), silent)
}

function assignIssue(issue: GitHubIssue) {
  const msg = `Work on issue #${issue.number} in ${issue.repo}: "${issue.title}"`
  inputText.value = msg
  activeTab.value = 'chat'
  nextTick(() => {
    (document.querySelector('.chat-input') as HTMLInputElement | null)?.focus()
  })
}

function formatTime(isoStr?: string) {
  if (!isoStr) return ''
  return new Date(isoStr).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function formatAge(isoStr?: string) {
  if (!isoStr) return ''
  const diffMins = Math.floor((Date.now() - new Date(isoStr).getTime()) / 60000)
  if (diffMins < 60) return `${diffMins}m`
  const diffHours = Math.floor(diffMins / 60)
  if (diffHours < 24) return `${diffHours}h`
  return `${Math.floor(diffHours / 24)}d`
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
  max-height: 500px;
  display: flex;
  flex-direction: column;
  z-index: 100;
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

.poll-indicator {
  font-size: 9px;
  color: var(--color-text-dim);
  opacity: 0.7;
  white-space: nowrap;
}

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

/* ── Tabs ── */
.tab-bar {
  display: flex;
  border-bottom: 2px solid var(--color-brass-dark);
  flex-shrink: 0;
}

.tab-btn {
  flex: 1;
  background: var(--color-bg-tertiary);
  border: none;
  border-right: 1px solid var(--color-brass-dark);
  color: var(--color-text-dim);
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 1px;
  padding: 7px 10px;
  cursor: pointer;
  transition: all 0.15s;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
}

.tab-btn:last-child {
  border-right: none;
}

.tab-btn:hover {
  background: rgba(232, 170, 0, 0.08);
  color: var(--color-text);
}

.tab-btn.active {
  background: var(--color-bg-secondary);
  color: var(--color-brass-light);
  border-bottom: 2px solid var(--color-brass);
  margin-bottom: -2px;
}

.badge {
  background: var(--color-teal);
  color: var(--color-bg);
  border-radius: 2px;
  font-size: 6px;
  padding: 1px 4px;
  min-width: 14px;
  text-align: center;
}

/* ── Chat messages ── */
.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 180px;
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
  gap: 4px;
  padding: 6px 10px;
  border-radius: 2px;
  max-width: 90%;
}

/* ── User messages: brass/gold, right-aligned ── */
.chat-message.from-user {
  align-self: flex-end;
  background: rgba(232, 170, 0, 0.15);
  border: 1px solid var(--color-brass-dark);
  border-right: 3px solid var(--color-brass);
}

/* ── Generic agent messages ── */
.chat-message.from-agent {
  align-self: flex-start;
  background: rgba(0, 187, 170, 0.08);
  border: 1px solid rgba(0, 187, 170, 0.25);
  border-left: 3px solid var(--color-teal);
}

/* ── Foreman messages: distinct teal panel ── */
.chat-message.from-foreman {
  align-self: flex-start;
  background: rgba(0, 187, 170, 0.1);
  border: 1px solid rgba(0, 187, 170, 0.35);
  border-left: 3px solid var(--color-teal);
  max-width: 95%;
}

/* ── Tool-use / tool-result blocks ── */
.chat-message.msg-tool {
  align-self: flex-start;
  background: rgba(0, 0, 0, 0.25);
  border: 1px solid rgba(232, 170, 0, 0.15);
  border-left: 3px solid var(--color-brass-dark);
  max-width: 95%;
  padding: 5px 8px;
}

/* ── System messages: muted/subdued ── */
.chat-message.from-system {
  align-self: center;
  background: rgba(80, 80, 80, 0.08);
  border: 1px solid rgba(120, 120, 120, 0.2);
  max-width: 95%;
}

/* ── Message header row (sender + time) ── */
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

/* ── Tool blocks ── */
.tool-block {
  width: 100%;
}

.tool-block-header {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  background: none;
  border: none;
  padding: 2px 0;
  cursor: pointer;
  text-align: left;
  color: inherit;
}

.tool-block-header:hover {
  opacity: 0.85;
}

.tool-block--use .tool-block-header {
  color: var(--color-amber);
}

.tool-block--result .tool-block-header {
  color: var(--color-teal);
}

.tool-block--error .tool-block-header {
  color: var(--color-red);
}

.tool-name {
  font-family: var(--font-mono);
  font-size: 11px;
  flex: 1;
  text-align: left;
}

.tool-expand-icon {
  font-size: 7px;
  color: var(--color-text-dim);
  opacity: 0.6;
  flex-shrink: 0;
}

.tool-preview {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--color-text-dim);
  opacity: 0.55;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  flex: 1;
  margin-left: 8px;
}

.tool-body {
  margin-top: 4px;
  border-left: 2px solid rgba(232, 170, 0, 0.2);
  padding-left: 8px;
}

.tool-block--result .tool-body {
  border-left-color: rgba(0, 187, 170, 0.2);
}

.tool-json,
.tool-output {
  margin: 0;
  font-family: var(--font-mono);
  font-size: 10px;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 200px;
  overflow-y: auto;
  background: rgba(0, 0, 0, 0.35);
  border: 1px solid rgba(42, 26, 5, 0.8);
  padding: 6px 8px;
}

.tool-json {
  color: var(--color-amber);
}

.tool-output {
  color: var(--color-text-dim);
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

/* ── Issues tab ── */
.issues-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--color-brass-dark);
  flex-shrink: 0;
}

.refresh-btn {
  font-size: 10px;
  padding: 3px 7px;
}

.refresh-btn:disabled {
  opacity: 0.4;
  pointer-events: none;
}

.issues-count {
  font-size: 11px;
  color: var(--color-text-dim);
}

.issues-repos {
  margin-left: auto;
  font-size: 10px;
  color: var(--color-text-dim);
}

.issues-list {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  min-height: 180px;
  max-height: 380px;
}

.issue-row {
  padding: 9px 12px;
  border-bottom: 1px solid var(--color-bg-tertiary);
  cursor: pointer;
  transition: background 0.1s;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.issue-row:hover {
  background: rgba(0, 187, 170, 0.07);
}

.issue-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.issue-repo {
  font-size: 9px;
  color: var(--color-text-dim);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 220px;
}

.issue-number {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-brass-dark);
}

.issue-title {
  font-size: 12px;
  color: var(--color-text);
  line-height: 1.35;
}

.issue-meta {
  display: flex;
  align-items: center;
  gap: 5px;
  flex-wrap: wrap;
}

.issue-label {
  font-size: 9px;
  border: 1px solid;
  padding: 1px 5px;
  border-radius: 2px;
}

.issue-age {
  margin-left: auto;
  font-size: 9px;
  color: var(--color-text-dim);
}

/* ── Claude auth modal (teleported to body) ── */
.auth-modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.8);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 500;
}

.auth-modal {
  background: var(--color-bg-secondary, #1a1a2e);
  border: 3px solid var(--color-red, #c0392b);
  box-shadow: 0 0 40px rgba(192, 57, 43, 0.4), 0 0 80px rgba(192, 57, 43, 0.15);
  width: 520px;
  max-width: calc(100vw - 32px);
  display: flex;
  flex-direction: column;
}

.auth-modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  background: rgba(192, 57, 43, 0.15);
  border-bottom: 2px solid var(--color-red, #c0392b);
  flex-shrink: 0;
}

.auth-modal-title {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-red, #e74c3c);
  letter-spacing: 2px;
  text-shadow: 0 0 8px rgba(231, 76, 60, 0.6);
}

.auth-modal-worker {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--color-text-dim);
}

.auth-modal-body {
  padding: 20px 20px 16px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.auth-instruction {
  margin: 0;
  font-size: 12px;
  color: var(--color-text);
  line-height: 1.5;
}

.auth-url-block {
  display: block;
  background: var(--color-bg, #0d0d1a);
  border: 1px solid var(--color-border, #333);
  padding: 8px 10px;
  color: var(--color-teal, #00bcd4);
  font-family: var(--font-mono);
  font-size: 11px;
  word-break: break-all;
  text-decoration: none;
  line-height: 1.4;
}

.auth-url-block:hover {
  border-color: var(--color-teal, #00bcd4);
  text-decoration: underline;
}

.auth-input-row {
  display: flex;
  gap: 8px;
}

.auth-input {
  flex: 1;
  background: var(--color-bg, #0d0d1a);
  border: 2px solid var(--color-red, #c0392b);
  color: var(--color-text);
  font-family: var(--font-mono);
  font-size: 13px;
  padding: 7px 10px;
  outline: none;
}

.auth-input:focus {
  border-color: var(--color-red, #e74c3c);
  box-shadow: 0 0 10px rgba(231, 76, 60, 0.4);
}

.auth-input::placeholder {
  color: var(--color-text-dim);
  font-style: italic;
}

.auth-submit-btn {
  padding: 7px 14px;
  font-size: 11px;
  border-color: var(--color-red, #c0392b);
  color: var(--color-red, #e74c3c);
  white-space: nowrap;
}

.auth-submit-btn:hover:not(:disabled) {
  background: rgba(192, 57, 43, 0.25);
}

.auth-submit-btn:disabled {
  opacity: 0.4;
  pointer-events: none;
}

@media (max-width: 1024px) {
  .chat-pane {
    max-height: 100% !important;
    border-radius: 0;
  }

  .chat-messages {
    min-height: 0;
    max-height: none;
    flex: 1;
  }

  .issues-list {
    min-height: 0;
    max-height: none;
  }
}
</style>
