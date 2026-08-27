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
      <div class="pane-tabs">
        <button
          class="pane-tab"
          :class="{ active: activeTab === 'chat' }"
          @click="activeTab = 'chat'"
        >
          Chat
        </button>
        <button
          class="pane-tab"
          :class="{ active: activeTab === 'conversations' }"
          @click="activeTab = 'conversations'"
        >
          Conversations
          <span v-if="activeConversationCount" class="tab-count">{{
            activeConversationCount
          }}</span>
        </button>
      </div>
      <ChatTab v-if="activeTab === 'chat'" ref="chatTabRef" />
      <ThreadList v-else />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useGuildStore } from '../stores/guild'
import { useAgentsStore } from '../stores/agents'
import { useThreadsStore } from '../stores/threads'
import ChatTab from './chat-pane/ChatTab.vue'
import ThreadList from './sidebar/ThreadList.vue'

const guildStore = useGuildStore()
const agentsStore = useAgentsStore()
const threadsStore = useThreadsStore()

const minimized = ref(false)
const activeTab = ref<'chat' | 'conversations'>('chat')
const chatTabRef = ref<InstanceType<typeof ChatTab> | null>(null)

const foreman = computed(() => agentsStore.agents.find((a) => a.type === 'foreman'))
const activeConversationCount = computed(
  () => threadsStore.threads.filter((t) => t.status === 'active').length,
)

// Poll countdown: guildStore.nextPollAt holds the epoch ms when the next foreman
// check fires (set from foreman-poll-status WS frames); pollTick forces this
// computed to re-evaluate every 30 s so the "Xm" label keeps counting down.
const pollTick = ref(0)
let pollCountdownTimer: ReturnType<typeof setInterval> | null = null

const pollLabel = computed(() => {
  void pollTick.value // subscribe so re-render fires on each tick
  if (guildStore.nextPollAt === null) return ''
  const remaining = Math.max(0, Math.ceil((guildStore.nextPollAt - Date.now()) / 1000))
  if (remaining <= 0) return 'checking...'
  const mins = Math.ceil(remaining / 60)
  return `next check in ${mins}m`
})

function toggleMinimize() {
  minimized.value = !minimized.value
}

onMounted(() => {
  pollCountdownTimer = setInterval(() => {
    pollTick.value++
  }, 30_000)
})

onUnmounted(() => {
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

.pane-tabs {
  display: flex;
  border-bottom: 1px solid var(--color-brass-dark);
  background: var(--color-bg-tertiary);
  flex-shrink: 0;
}

.pane-tab {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  padding: 7px 4px;
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
  cursor: pointer;
  color: var(--color-text-dim);
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 1px;
  text-transform: uppercase;
}

.pane-tab.active {
  color: var(--color-brass-light);
  border-bottom-color: var(--color-brass);
  background: rgba(232, 170, 0, 0.08);
}

.tab-count {
  min-width: 16px;
  padding: 1px 4px;
  border-radius: 8px;
  background: var(--color-brass-dark);
  color: var(--color-bg);
  font-family: var(--font-mono);
  font-size: 9px;
  font-weight: bold;
}

@media (max-width: 1024px) {
  .chat-pane {
    border-left: none;
    border-radius: 0;
  }
}
</style>
