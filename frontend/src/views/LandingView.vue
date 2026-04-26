<template>
  <div class="landing">
    <div class="landing-bg">
      <div class="gear gear-1"></div>
      <div class="gear gear-2"></div>
      <div class="pipe pipe-h pipe-top"></div>
      <div class="pipe pipe-h pipe-bottom"></div>
      <div class="sparkles">
        <span v-for="n in 12" :key="n" class="sparkle" :style="sparkleStyle(n)"></span>
      </div>
    </div>

    <div class="landing-content">
      <div class="landing-header">
        <div class="logo-block">
          <div class="logo-title">PIONEER SQUARE</div>
          <div class="logo-subtitle">Multi-Agent Coding Workshop</div>
        </div>
        <div class="header-actions">
          <button class="pixel-btn new-btn" @click="openNewSession">+ NEW SESSION</button>
        </div>
      </div>

      <div class="sessions-section">
        <div class="section-label">ACTIVE SESSIONS</div>

        <div v-if="loading" class="loading-msg">Loading sessions...</div>

        <div v-else-if="sessions.length === 0" class="empty-state">
          <div class="empty-icon">⚙</div>
          <div class="empty-text">No sessions running.</div>
          <div class="empty-sub">Start one to begin coordinating agents.</div>
          <button class="pixel-btn" @click="openNewSession">+ NEW SESSION</button>
        </div>

        <div v-else class="sessions-grid">
          <div
            v-for="session in sessions"
            :key="session.id"
            class="session-card"
            @click="goToSession(session.id)"
          >
            <div class="card-top">
              <span class="card-id">{{ session.id }}</span>
              <span class="card-agents">
                <span class="agent-dot" :class="session.agent_count > 0 ? 'active' : 'empty'"></span>
                {{ session.agent_count || 0 }} agent{{ session.agent_count !== 1 ? 's' : '' }}
              </span>
            </div>
            <div class="card-name">{{ session.name }}</div>
            <div class="card-time">Created {{ formatTime(session.created_at) }}</div>
            <div class="card-enter">ENTER →</div>
          </div>
        </div>
      </div>
    </div>

    <!-- New Session Modal -->
    <div v-if="showNewModal" class="modal-overlay" @click.self="showNewModal = false">
      <div class="modal">
        <div class="modal-header">NEW SESSION</div>
        <div class="modal-body">
          <label class="field-label">Session Name (optional)</label>
          <input
            v-model="newSessionName"
            class="field-input"
            placeholder="e.g. Feature Sprint #4"
            @keydown.enter="createSession"
            ref="nameInput"
          />
        </div>
        <div class="modal-footer">
          <button class="pixel-btn cancel-btn" @click="showNewModal = false">Cancel</button>
          <button class="pixel-btn" @click="createSession" :disabled="creating">
            {{ creating ? 'Creating...' : 'CREATE' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { useSessionStore } from '../stores/session.js'

const router = useRouter()
const sessionStore = useSessionStore()

const loading = ref(true)
const sessions = ref([])
const showNewModal = ref(false)
const newSessionName = ref('')
const creating = ref(false)
const nameInput = ref(null)

onMounted(async () => {
  await sessionStore.loadSessions()
  sessions.value = sessionStore.sessions
  loading.value = false
})

function goToSession(id) {
  router.push(`/${id}`)
}

async function openNewSession() {
  showNewModal.value = true
  newSessionName.value = ''
  await nextTick()
  nameInput.value?.focus()
}

async function createSession() {
  if (creating.value) return
  creating.value = true
  try {
    const session = await sessionStore.createSession(newSessionName.value.trim() || undefined)
    router.push(`/${session.id}`)
  } finally {
    creating.value = false
  }
}

function formatTime(isoStr) {
  if (!isoStr) return ''
  const d = new Date(isoStr)
  const now = new Date()
  const diffMs = now - d
  const diffMins = Math.floor(diffMs / 60000)
  if (diffMins < 1) return 'just now'
  if (diffMins < 60) return `${diffMins}m ago`
  const diffHours = Math.floor(diffMins / 60)
  if (diffHours < 24) return `${diffHours}h ago`
  return d.toLocaleDateString()
}

function sparkleStyle(n) {
  const seed = n * 137.508
  return {
    left: `${(seed * 7.3) % 100}%`,
    top: `${(seed * 3.7) % 100}%`,
    animationDelay: `${(n * 0.4) % 3}s`,
    animationDuration: `${2 + (n % 3)}s`,
  }
}
</script>

<style scoped>
.landing {
  width: 100vw;
  height: 100vh;
  background: var(--color-bg);
  display: flex;
  align-items: stretch;
  justify-content: center;
  position: relative;
  overflow: hidden;
}

/* ── Background decorations ── */
.landing-bg {
  position: absolute;
  inset: 0;
  pointer-events: none;
}

.gear {
  position: absolute;
  border-radius: 50%;
  border: 6px solid var(--color-brass-dark);
  opacity: 0.12;
}
.gear::before, .gear::after {
  content: '';
  position: absolute;
  background: var(--color-brass-dark);
}
.gear-1 {
  width: 260px;
  height: 260px;
  bottom: -60px;
  left: -60px;
  animation: spin 30s linear infinite;
}
.gear-2 {
  width: 180px;
  height: 180px;
  top: -40px;
  right: 120px;
  animation: spin 20s linear infinite reverse;
}

.pipe {
  position: absolute;
  background: linear-gradient(180deg, var(--color-brass-dark) 0%, #6b4f00 50%, var(--color-brass-dark) 100%);
  opacity: 0.15;
}
.pipe-h {
  height: 18px;
  left: 0;
  right: 0;
}
.pipe-top { top: 0; }
.pipe-bottom { bottom: 0; }

.sparkle {
  position: absolute;
  width: 3px;
  height: 3px;
  background: var(--color-brass-light);
  border-radius: 50%;
  animation: twinkle 2s ease-in-out infinite;
}

@keyframes spin { to { transform: rotate(360deg); } }
@keyframes twinkle {
  0%, 100% { opacity: 0.1; transform: scale(1); }
  50% { opacity: 0.9; transform: scale(1.6); }
}

/* ── Content ── */
.landing-content {
  position: relative;
  z-index: 1;
  width: 100%;
  max-width: 960px;
  padding: 40px 32px;
  display: flex;
  flex-direction: column;
  gap: 36px;
}

.landing-header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  border-bottom: 2px solid var(--color-brass-dark);
  padding-bottom: 20px;
}

.logo-title {
  font-family: var(--font-pixel);
  font-size: 22px;
  color: var(--color-brass-light);
  text-shadow: 0 0 20px rgba(255, 214, 68, 0.5), 0 0 40px rgba(255, 214, 68, 0.2);
  letter-spacing: 4px;
  margin-bottom: 8px;
}

.logo-subtitle {
  font-size: 13px;
  color: var(--color-text-dim);
  letter-spacing: 2px;
}

.new-btn {
  font-size: 9px;
  padding: 10px 16px;
}

/* ── Sessions ── */
.sessions-section {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 16px;
  overflow: hidden;
}

.section-label {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-brass);
  letter-spacing: 3px;
  opacity: 0.7;
}

.loading-msg {
  color: var(--color-text-dim);
  font-size: 12px;
  padding: 20px 0;
}

.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  padding: 60px 20px;
  color: var(--color-text-dim);
}

.empty-icon {
  font-size: 40px;
  opacity: 0.3;
  animation: spin 8s linear infinite;
}

.empty-text {
  font-size: 14px;
  color: var(--color-text);
}

.empty-sub {
  font-size: 12px;
  margin-bottom: 12px;
}

.sessions-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 16px;
  overflow-y: auto;
  padding-bottom: 20px;
}

.session-card {
  background: var(--color-bg-secondary);
  border: 2px solid var(--color-brass-dark);
  padding: 16px 18px;
  cursor: pointer;
  transition: all 0.15s;
  display: flex;
  flex-direction: column;
  gap: 6px;
  position: relative;
}

.session-card:hover {
  border-color: var(--color-brass);
  background: var(--color-bg-tertiary);
  box-shadow: 0 0 16px rgba(232, 170, 0, 0.2);
  transform: translateY(-2px);
}

.card-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.card-id {
  font-family: var(--font-pixel);
  font-size: 8px;
  color: var(--color-brass);
}

.card-agents {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 10px;
  color: var(--color-text-dim);
}

.agent-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--color-text-dim);
}

.agent-dot.active {
  background: var(--color-green);
  box-shadow: 0 0 6px var(--color-green);
  animation: pulse 2s infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

.card-name {
  font-size: 14px;
  color: var(--color-text);
  font-weight: bold;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.card-time {
  font-size: 10px;
  color: var(--color-text-dim);
}

.card-enter {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-teal);
  opacity: 0;
  transition: opacity 0.15s;
  align-self: flex-end;
  margin-top: 4px;
}

.session-card:hover .card-enter {
  opacity: 1;
}

/* ── Modal ── */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.7);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 200;
}

.modal {
  background: var(--color-bg-secondary);
  border: 3px solid var(--color-brass);
  box-shadow: 0 0 30px rgba(232, 170, 0, 0.3);
  width: 380px;
  display: flex;
  flex-direction: column;
}

.modal-header {
  font-family: var(--font-pixel);
  font-size: 9px;
  color: var(--color-brass-light);
  letter-spacing: 2px;
  padding: 14px 18px;
  background: var(--color-bg-tertiary);
  border-bottom: 2px solid var(--color-brass-dark);
}

.modal-body {
  padding: 20px 18px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.field-label {
  font-size: 10px;
  color: var(--color-text-dim);
  letter-spacing: 1px;
}

.field-input {
  background: var(--color-bg);
  border: 2px solid var(--color-brass-dark);
  color: var(--color-text);
  font-family: var(--font-mono);
  font-size: 13px;
  padding: 8px 10px;
  outline: none;
  width: 100%;
}

.field-input:focus {
  border-color: var(--color-brass);
  box-shadow: 0 0 8px rgba(232, 170, 0, 0.3);
}

.field-input::placeholder {
  color: var(--color-text-dim);
}

.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 14px 18px;
  border-top: 2px solid var(--color-brass-dark);
  background: var(--color-bg-tertiary);
}

.cancel-btn {
  background: transparent;
  border-color: var(--color-brass-dark);
  color: var(--color-text-dim);
}

.cancel-btn:hover {
  background: rgba(255, 255, 255, 0.05);
  box-shadow: none;
}
</style>
