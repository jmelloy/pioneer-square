<template>
  <div class="chat-pane panel-bg" :class="{ minimized }">
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
            :class="{
              'from-user': msg.from === 'user',
              'from-system': msg.from === 'system',
              'from-agent': msg.from !== 'user' && msg.from !== 'system'
            }"
          >
            <span class="msg-from">{{ msg.from === 'user' ? 'YOU' : msg.from === 'system' ? 'SYS' : msg.from }}</span>
            <span class="msg-content">{{ msg.content }}</span>
            <a v-if="msg.prUrl" :href="msg.prUrl" target="_blank" rel="noopener" class="pr-link">
              Open PR →
            </a>
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
      </template>

      <!-- Issues tab -->
      <template v-else-if="activeTab === 'issues'">
        <div class="issues-toolbar">
          <button class="pixel-btn refresh-btn" @click="refreshIssues" :disabled="ghStore.loading">
            {{ ghStore.loading ? '...' : '↻' }}
          </button>
          <span class="issues-count">{{ ghStore.issues.length }} open</span>
          <span class="issues-repos">{{ ghStore.selectedRepos.length }} repo{{ ghStore.selectedRepos.length !== 1 ? 's' : '' }}</span>
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
</template>

<script setup>
import { ref, computed, nextTick, watch, onMounted, onUnmounted } from 'vue'
import { useGuildStore } from '../stores/guild.js'
import { useAgentsStore } from '../stores/agents.js'
import { useGitHubStore } from '../stores/github.js'

const guildStore = useGuildStore()
const agentsStore = useAgentsStore()
const ghStore = useGitHubStore()

const minimized = ref(false)
const inputText = ref('')
const messagesEl = ref(null)
const issuesEl = ref(null)
const activeTab = ref('chat')

const messages = computed(() => guildStore.messages)
const foreman = computed(() => agentsStore.agents.find(a => a.type === 'foreman'))

// Issue assignment pattern: "Work on issue #N in owner/repo: title"
const ISSUE_PATTERN = /Work on issue #(\d+) in ([^:]+): "(.+)"/

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
    const [, issueNum, repoName, title] = match
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
      } catch (e) {
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

// Listen for task lifecycle + escalation broadcasts
function handleTaskEvent(data) {
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
  } else if (data.type === 'task-assigned') {
    guildStore.messages.push({
      type: 'chat', from: 'system', to: 'user',
      content: `→ ${data.workerId} assigned: ${data.description}`,
      createdAt: new Date().toISOString(),
    })
  }
}

onMounted(() => guildStore.addMessageHandler(handleTaskEvent))
onUnmounted(() => guildStore.removeMessageHandler(handleTaskEvent))

async function switchToIssues() {
  activeTab.value = 'issues'
  if (ghStore.issues.length === 0 && !ghStore.loading) {
    await ghStore.fetchIssues()
  }
}

async function refreshIssues() {
  await ghStore.fetchIssues()
}

function assignIssue(issue) {
  const msg = `Work on issue #${issue.number} in ${issue.repo}: "${issue.title}"`
  inputText.value = msg
  activeTab.value = 'chat'
  nextTick(() => {
    document.querySelector('.chat-input')?.focus()
  })
}

function formatTime(isoStr) {
  if (!isoStr) return ''
  return new Date(isoStr).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function formatAge(isoStr) {
  if (!isoStr) return ''
  const diffMins = Math.floor((Date.now() - new Date(isoStr)) / 60000)
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

.chat-message.from-system {
  align-self: center;
  background: rgba(255, 204, 0, 0.06);
  border: 1px solid rgba(255, 204, 0, 0.2);
  border-left: 3px solid var(--color-amber);
  max-width: 95%;
}

.from-system .msg-from {
  color: var(--color-amber);
}

.from-system .msg-content {
  font-size: 11px;
  color: var(--color-text-dim);
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
</style>
