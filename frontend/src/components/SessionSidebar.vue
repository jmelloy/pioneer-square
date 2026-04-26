<template>
  <aside class="sidebar">
    <div class="sidebar-header">
      <span class="sidebar-title">Sessions</span>
      <button class="pixel-btn new-btn" @click="newSession">+ New</button>
    </div>
    <div class="session-list">
      <div
        v-for="session in sessions"
        :key="session.id"
        class="session-item"
        :class="{ active: currentSession && currentSession.id === session.id }"
        @click="goToSession(session.id)"
      >
        <div class="session-id">{{ session.id }}</div>
        <div class="session-name">{{ session.name }}</div>
        <div class="session-time">{{ formatTime(session.created_at) }}</div>
      </div>
      <div v-if="sessions.length === 0" class="no-sessions">No sessions yet</div>
    </div>
    <div class="sidebar-footer">
      <div class="connection-status" :class="{ connected: isConnected }">
        <span class="status-dot"></span>
        {{ isConnected ? 'Connected' : 'Disconnected' }}
      </div>
    </div>
  </aside>
</template>

<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useSessionStore } from '../stores/session.js'

const router = useRouter()
const sessionStore = useSessionStore()

const sessions = computed(() => sessionStore.sessions)
const currentSession = computed(() => sessionStore.currentSession)
const isConnected = computed(() => sessionStore.isConnected)

async function newSession() {
  const session = await sessionStore.createSession()
  router.push(`/${session.id}`)
}

function goToSession(id) {
  router.push(`/${id}`)
}

function formatTime(isoStr) {
  if (!isoStr) return ''
  const d = new Date(isoStr)
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}
</script>

<style scoped>
.sidebar {
  width: 220px;
  min-width: 220px;
  background: var(--color-bg-secondary);
  border-right: 2px solid var(--color-brass-dark);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.sidebar-header {
  padding: 12px 10px;
  border-bottom: 2px solid var(--color-brass-dark);
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: linear-gradient(135deg, var(--color-bg-tertiary), rgba(192, 122, 255, 0.15));
}

.sidebar-title {
  font-family: var(--font-pixel);
  font-size: 7px;
  letter-spacing: 2px;
  text-transform: uppercase;
  background: linear-gradient(90deg, var(--color-pink), var(--color-purple));
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
  color: transparent;
}

.new-btn {
  font-size: 7px;
  padding: 4px 7px;
}

.session-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
}

.session-item {
  padding: 10px 12px;
  cursor: pointer;
  border-bottom: 1px solid var(--color-bg-tertiary);
  transition: background 0.15s;
}

.session-item:hover {
  background: var(--color-bg-tertiary);
}

.session-item.active {
  background: rgba(192, 122, 255, 0.12);
  border-left: 3px solid var(--color-brass);
  box-shadow: inset 0 0 8px rgba(192, 122, 255, 0.1);
}

.session-id {
  font-family: var(--font-pixel);
  font-size: 9px;
  color: var(--color-brass-light);
  margin-bottom: 3px;
}

.session-name {
  font-size: 11px;
  color: var(--color-text);
  margin-bottom: 2px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.session-time {
  font-size: 10px;
  color: var(--color-text-dim);
}

.no-sessions {
  padding: 20px;
  text-align: center;
  color: var(--color-text-dim);
  font-size: 11px;
}

.sidebar-footer {
  padding: 10px 12px;
  border-top: 2px solid var(--color-brass-dark);
  background: linear-gradient(135deg, var(--color-bg-tertiary), rgba(255, 107, 179, 0.08));
}

.connection-status {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 10px;
  color: var(--color-red);
}

.connection-status.connected {
  color: var(--color-green);
}

.status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
  display: inline-block;
}

.connection-status.connected .status-dot {
  animation: pulse 2s infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.3; }
}
</style>
