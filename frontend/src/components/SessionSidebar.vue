<template>
  <aside class="sidebar panel-bg">
    <div class="sidebar-header">
      <span class="sidebar-title">Sessions</span>
      <button class="pixel-btn new-btn" @click="goHome">⌂ Home</button>
    </div>

    <div class="session-list">
      <div
        v-for="session in sessions"
        :key="session.id"
        class="session-item"
        :class="{ active: currentSession && currentSession.id === session.id }"
        @click="goToSession(session.id)"
      >
        <div class="session-top">
          <span class="session-id">{{ session.id }}</span>
          <span class="session-agents" v-if="session.agent_count !== undefined">
            <span class="agents-dot" :class="session.agent_count > 0 ? 'active' : ''"></span>
            {{ session.agent_count }}
          </span>
        </div>
        <div class="session-name">{{ session.name }}</div>
        <div class="session-time">{{ formatTime(session.created_at) }}</div>
      </div>
      <div v-if="sessions.length === 0" class="no-sessions">No sessions yet</div>
    </div>

    <div class="sidebar-footer">
      <!-- Deploy worker — only when in an active session -->
      <button
        v-if="currentSession"
        class="pixel-btn deploy-btn"
        @click="showDeployModal = true"
        title="Deploy a new worker agent"
      >+ WORKER</button>

      <!-- GitHub identity block -->
      <div class="gh-block" @click="showGitHubModal = true" :title="ghStore.isConfigured ? 'GitHub: ' + ghStore.user.login : 'Configure GitHub'">
        <div class="gh-inner" :class="{ configured: ghStore.isConfigured }">
          <img v-if="ghStore.isConfigured" :src="ghStore.user.avatar_url" class="gh-avatar" alt="gh" />
          <span v-else class="gh-icon">⚙</span>
          <div class="gh-text">
            <span v-if="ghStore.isConfigured" class="gh-login">{{ ghStore.user.login }}</span>
            <span v-else class="gh-setup">Connect GitHub</span>
            <span class="gh-repos" v-if="ghStore.isConfigured && ghStore.selectedRepos.length > 0">
              {{ ghStore.selectedRepos.length }} repo{{ ghStore.selectedRepos.length !== 1 ? 's' : '' }}
            </span>
          </div>
        </div>
      </div>

      <div class="connection-status" :class="{ connected: isConnected }">
        <span class="status-dot"></span>
        {{ isConnected ? 'Connected' : 'Disconnected' }}
      </div>
    </div>
  </aside>

  <GitHubConfigModal v-if="showGitHubModal" @close="showGitHubModal = false" />
  <DeployWorkerModal v-if="showDeployModal" @close="showDeployModal = false" />
</template>

<script setup>
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { useSessionStore } from '../stores/session.js'
import { useGitHubStore } from '../stores/github.js'
import GitHubConfigModal from './GitHubConfigModal.vue'
import DeployWorkerModal from './DeployWorkerModal.vue'

const router = useRouter()
const sessionStore = useSessionStore()
const ghStore = useGitHubStore()

const showGitHubModal = ref(false)
const showDeployModal = ref(false)
const currentSession = computed(() => sessionStore.currentSession)

const sessions = computed(() => sessionStore.sessions)
const isConnected = computed(() => sessionStore.isConnected)

function goHome() {
  router.push('/')
}

function goToSession(id) {
  router.push(`/${id}`)
}

function formatTime(isoStr) {
  if (!isoStr) return ''
  const d = new Date(isoStr)
  const now = new Date()
  const diffMins = Math.floor((now - d) / 60000)
  if (diffMins < 1) return 'just now'
  if (diffMins < 60) return `${diffMins}m ago`
  const diffHours = Math.floor(diffMins / 60)
  if (diffHours < 24) return `${diffHours}h ago`
  return d.toLocaleDateString()
}
</script>

<style scoped>
.sidebar {
  width: 220px;
  min-width: 220px;
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
  background: var(--color-bg-tertiary);
}

.sidebar-title {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-brass-light);
  letter-spacing: 2px;
  text-transform: uppercase;
  text-shadow: 0 0 6px rgba(255, 214, 68, 0.4);
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
  background: rgba(232, 170, 0, 0.12);
  border-left: 3px solid var(--color-brass);
}

.session-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 3px;
}

.session-id {
  font-family: var(--font-pixel);
  font-size: 9px;
  color: var(--color-brass-light);
}

.session-agents {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 10px;
  color: var(--color-text-dim);
}

.agents-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--color-text-dim);
  display: inline-block;
}

.agents-dot.active {
  background: var(--color-green);
  animation: pulse 2s infinite;
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

/* ── Footer ── */
.sidebar-footer {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px 12px;
  border-top: 2px solid var(--color-brass-dark);
  background: var(--color-bg-tertiary);
}

/* ── Deploy worker button ── */
.deploy-btn {
  width: 100%;
  font-size: 7px;
  padding: 6px 10px;
  background: linear-gradient(180deg, rgba(0, 187, 170, 0.3) 0%, rgba(0, 120, 110, 0.5) 100%);
  border-color: var(--color-teal);
  color: var(--color-teal);
}

.deploy-btn:hover {
  background: linear-gradient(180deg, rgba(0, 187, 170, 0.5) 0%, rgba(0, 150, 140, 0.7) 100%);
  box-shadow: 0 0 10px rgba(0, 187, 170, 0.4);
  color: var(--color-cream);
}

/* ── GitHub block ── */
.gh-block {
  cursor: pointer;
  border: 1px solid var(--color-brass-dark);
  transition: all 0.15s;
  border-radius: 2px;
}

.gh-block:hover {
  border-color: var(--color-brass);
  background: rgba(232, 170, 0, 0.06);
}

.gh-inner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 9px;
}

.gh-inner.configured {
  border-left: 3px solid var(--color-teal);
}

.gh-avatar {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  border: 1px solid var(--color-teal);
  flex-shrink: 0;
}

.gh-icon {
  font-size: 16px;
  color: var(--color-brass-dark);
  flex-shrink: 0;
}

.gh-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  overflow: hidden;
}

.gh-login {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-teal);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.gh-setup {
  font-size: 10px;
  color: var(--color-text-dim);
}

.gh-repos {
  font-size: 9px;
  color: var(--color-text-dim);
}

/* ── Connection status ── */
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
