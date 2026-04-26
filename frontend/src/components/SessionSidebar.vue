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

    <!-- GitHub config for the active session -->
    <div v-if="currentSession" class="gh-config">
      <div class="gh-config-header" @click="ghOpen = !ghOpen">
        <span class="gh-icon">⬡</span>
        <span class="gh-title">GitHub</span>
        <span class="gh-status-dot" :class="ghConfigured ? 'ok' : 'off'"></span>
        <span class="gh-toggle">{{ ghOpen ? '▲' : '▼' }}</span>
      </div>

      <div v-if="ghOpen" class="gh-config-body">
        <label class="gh-label">Token
          <input
            v-model="ghToken"
            class="gh-input"
            type="password"
            placeholder="ghp_…"
            autocomplete="off"
          />
        </label>
        <label class="gh-label">Repo
          <input v-model="ghRepo" class="gh-input" placeholder="owner/repo" />
        </label>
        <label class="gh-label">Project ID
          <input v-model="ghProjectId" class="gh-input" placeholder="PVT_…" />
        </label>
        <div class="gh-hint">Project node ID from GitHub Projects v2 URL</div>
        <div class="gh-actions">
          <button class="pixel-btn gh-save-btn" :disabled="saving" @click="saveConfig">
            {{ saving ? '…' : 'Save' }}
          </button>
          <span v-if="saveMsg" class="gh-save-msg" :class="saveError ? 'err' : 'ok'">
            {{ saveMsg }}
          </span>
        </div>
      </div>
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
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useSessionStore } from '../stores/session.js'

const router = useRouter()
const sessionStore = useSessionStore()

const sessions = computed(() => sessionStore.sessions)
const currentSession = computed(() => sessionStore.currentSession)
const isConnected = computed(() => sessionStore.isConnected)

// GitHub config fields — synced from currentSession
const ghOpen = ref(false)
const ghToken = ref('')
const ghRepo = ref('')
const ghProjectId = ref('')
const saving = ref(false)
const saveMsg = ref('')
const saveError = ref(false)

const ghConfigured = computed(() =>
  !!(currentSession.value?.github_token && currentSession.value?.github_repo)
)

// Populate inputs when the active session changes
watch(currentSession, (session) => {
  if (!session) return
  ghToken.value = session.github_token || ''
  ghRepo.value = session.github_repo || ''
  ghProjectId.value = session.github_project_id || ''
}, { immediate: true })

async function saveConfig() {
  saving.value = true
  saveMsg.value = ''
  saveError.value = false
  try {
    await sessionStore.updateSessionConfig({
      githubToken: ghToken.value.trim(),
      githubRepo: ghRepo.value.trim(),
      githubProjectId: ghProjectId.value.trim(),
    })
    saveMsg.value = 'Saved'
  } catch (e) {
    saveMsg.value = e.message
    saveError.value = true
  } finally {
    saving.value = false
    setTimeout(() => { saveMsg.value = '' }, 3000)
  }
}

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

/* ── GitHub config panel ─────────────────────────────────────── */
.gh-config {
  border-top: 2px solid var(--color-brass-dark);
  flex-shrink: 0;
}

.gh-config-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  cursor: pointer;
  background: var(--color-bg-tertiary);
  user-select: none;
  transition: background 0.12s;
}

.gh-config-header:hover {
  background: rgba(232, 170, 0, 0.06);
}

.gh-icon {
  font-size: 12px;
  color: var(--color-brass-dark);
}

.gh-title {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-text-dim);
  letter-spacing: 1px;
  flex: 1;
}

.gh-status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}
.gh-status-dot.ok  { background: var(--color-green); }
.gh-status-dot.off { background: var(--color-text-dim); }

.gh-toggle {
  font-size: 9px;
  color: var(--color-text-dim);
}

.gh-config-body {
  padding: 8px 12px 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  background: var(--color-bg);
  border-top: 1px solid var(--color-bg-tertiary);
}

.gh-label {
  display: flex;
  flex-direction: column;
  gap: 3px;
  font-size: 9px;
  color: var(--color-text-dim);
  letter-spacing: 0.5px;
  text-transform: uppercase;
}

.gh-input {
  background: var(--color-bg-secondary);
  border: 1px solid var(--color-brass-dark);
  color: var(--color-text);
  font-family: var(--font-mono);
  font-size: 10px;
  padding: 4px 6px;
  outline: none;
  width: 100%;
  box-sizing: border-box;
}
.gh-input:focus {
  border-color: var(--color-brass);
}
.gh-input::placeholder {
  color: var(--color-text-dim);
  opacity: 0.5;
}

.gh-hint {
  font-size: 9px;
  color: var(--color-text-dim);
  opacity: 0.6;
  font-style: italic;
}

.gh-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 2px;
}

.gh-save-btn {
  font-size: 8px;
  padding: 4px 10px;
}

.gh-save-msg {
  font-size: 10px;
}
.gh-save-msg.ok  { color: var(--color-green); }
.gh-save-msg.err { color: var(--color-red); }

/* ── Footer ──────────────────────────────────────────────────── */
.sidebar-footer {
  padding: 10px 12px;
  border-top: 2px solid var(--color-brass-dark);
  background: var(--color-bg-tertiary);
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
  50%       { opacity: 0.3; }
}
</style>
