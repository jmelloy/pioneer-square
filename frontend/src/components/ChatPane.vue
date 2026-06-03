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
        <span class="minimize-btn">{{ minimized ? '▶' : '◀' }}</span>
      </div>
    </div>

    <div v-if="!minimized" class="chat-body">
      <ChatTab ref="chatTabRef" />
    </div>
  </div>

  <ClaudeAuthModal :pending="claudeAuthPending" @submit="onSubmitAuth" />
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useGuildStore } from '../stores/guild'
import { useAgentsStore } from '../stores/agents'
import { api } from '../utils/api'
import type { GitHubIssue, WSInbound } from '../types'
import ChatTab from './chat-pane/ChatTab.vue'
import ClaudeAuthModal from './chat-pane/ClaudeAuthModal.vue'

const guildStore = useGuildStore()
const agentsStore = useAgentsStore()

const minimized = ref(false)
const chatTabRef = ref<InstanceType<typeof ChatTab> | null>(null)
const claudeAuthPending = ref<{ workerId: string; url: string } | null>(null)

const foreman = computed(() => agentsStore.agents.find((a) => a.type === 'foreman'))

// Poll countdown: epoch ms when next foreman check fires, plus a tick counter
// to force the computed to re-evaluate every 30 s.
const nextPollAt = ref<number | null>(null)
const pollTick = ref(0)
let pollCountdownTimer: ReturnType<typeof setInterval> | null = null

const pollLabel = computed(() => {
  void pollTick.value // subscribe so re-render fires on each tick
  if (nextPollAt.value === null) return ''
  const remaining = Math.max(0, Math.ceil((nextPollAt.value - Date.now()) / 1000))
  if (remaining <= 0) return 'checking...'
  const mins = Math.ceil(remaining / 60)
  return `next check in ${mins}m`
})

function toggleMinimize() {
  minimized.value = !minimized.value
}

function selectIssue(issue: GitHubIssue) {
  const msg = `Work on issue #${issue.number} in ${issue.repo}: "${issue.title}"`
  chatTabRef.value?.setInput(msg)
  chatTabRef.value?.focusInput()
}

defineExpose({ selectIssue })

function onSubmitAuth({ workerId, code }: { workerId: string; code: string }) {
  const sent = guildStore.sendMessage({
    type: 'worker-auth-response',
    workerId,
    code,
  })
  if (!sent) {
    guildStore.messages.push({
      type: 'chat',
      from: 'system',
      to: 'user',
      content: '⚠ Connection not ready — please wait a moment and try again.',
      createdAt: new Date().toISOString(),
    })
    return
  }
  claudeAuthPending.value = null
}

function handleTaskEvent(data: WSInbound) {
  if (data.type === 'task-complete') {
    guildStore.messages.push({
      type: 'chat',
      from: 'system',
      to: 'user',
      content: data.prUrl
        ? `✓ ${agentsStore.workerDisplayName(data.workerId)} done — PR: ${data.prUrl}`
        : `✓ ${agentsStore.workerDisplayName(data.workerId)} finished (no PR)`,
      prUrl: data.prUrl || null,
      createdAt: new Date().toISOString(),
    })
  } else if (data.type === 'needs-input') {
    guildStore.messages.push({
      type: 'chat',
      from: 'system',
      to: 'user',
      content: `⚠ ${agentsStore.workerDisplayName(data.workerId)} needs attention on: "${data.description}"`,
      createdAt: new Date().toISOString(),
    })
  } else if (data.type === 'claude-auth-required') {
    claudeAuthPending.value = { workerId: data.workerId, url: data.url }
    guildStore.messages.push({
      type: 'chat',
      from: 'system',
      to: 'user',
      content: `⚿ ${agentsStore.workerDisplayName(data.workerId)} needs Claude auth — visit the URL and paste the code below`,
      createdAt: new Date().toISOString(),
    })
  } else if (data.type === 'pi-auth-required') {
    guildStore.messages.push({
      type: 'chat',
      from: 'system',
      to: 'user',
      content: `⚿ ${agentsStore.workerDisplayName(data.workerId)} needs pi auth — visit the URL and paste the code in the worker auth panel`,
      createdAt: new Date().toISOString(),
    })
  } else if (data.type === 'task-assigned') {
    const taskName = (data.name || data.description || '').slice(0, 60)
    guildStore.messages.push({
      type: 'chat',
      from: 'system',
      to: 'user',
      content: `→ ${agentsStore.workerDisplayName(data.workerId)} assigned: ${taskName}`,
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
      const items = await api<Array<{ workerId: string; url: string }>>(
        `/guilds/${guildId}/pending-auth`,
      )
      if (items.length > 0) {
        claudeAuthPending.value = { workerId: items[0].workerId, url: items[0].url }
      }
    } catch (e) {
      console.warn('Could not fetch pending auth state', e)
    }
  }

  pollCountdownTimer = setInterval(() => {
    pollTick.value++
  }, 30_000)
})

onUnmounted(() => {
  guildStore.removeMessageHandler(handleTaskEvent)
  if (pollCountdownTimer !== null) {
    clearInterval(pollCountdownTimer)
    pollCountdownTimer = null
  }
})
</script>

<style scoped>
.chat-pane {
  width: 360px;
  min-width: 360px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border-left: 2px solid var(--color-brass-dark);
  transition:
    width 0.2s ease,
    min-width 0.2s ease;
}

.chat-pane.minimized {
  width: 44px;
  min-width: 44px;
}

.chat-pane.minimized .chat-title,
.chat-pane.minimized .agent-status,
.chat-pane.minimized .poll-indicator {
  display: none;
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

.status-dot.idle {
  background: var(--color-text-dim);
}
.status-dot.thinking {
  background: var(--color-blue);
}
.status-dot.working {
  background: var(--color-green);
}
.status-dot.busy {
  background: var(--color-orange);
}
.status-dot.error {
  background: var(--color-red);
}

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

@media (max-width: 1024px) {
  .chat-pane {
    border-left: none;
    border-radius: 0;
  }
}
</style>
