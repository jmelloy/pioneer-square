<template>
  <div class="modal-overlay" @click.self="$emit('close')">
    <div class="modal">
      <div class="modal-header">
        <span class="modal-title">⚙ GITHUB CONFIGURATION</span>
        <button class="close-btn" @click="$emit('close')">✕</button>
      </div>

      <div class="modal-body">
        <div class="user-card">
          <img :src="authStore.user?.avatar_url" class="avatar" alt="avatar" />
          <div class="user-info">
            <div class="user-name">{{ authStore.user?.login }}</div>
            <div class="user-label">{{ authStore.user?.name || 'GitHub User' }}</div>
          </div>
        </div>

        <div class="section">
          <div class="section-title">Connected Accounts</div>

          <div v-if="loading" class="loading-row">Loading…</div>

          <div v-else-if="logins.length === 0" class="empty-row">No connected accounts found.</div>

          <div v-else class="login-list">
            <div v-for="login in logins" :key="login.provider" class="login-row">
              <div class="login-icon">
                <span v-if="login.provider === 'github'" class="provider-icon">⬡</span>
                <span v-else class="provider-icon">◈</span>
              </div>
              <div class="login-info">
                <div class="login-provider">{{ providerLabel(login.provider) }}</div>
                <div class="login-username">{{ login.username }}</div>
              </div>
              <div class="login-actions">
                <button
                  class="disconnect-btn"
                  :disabled="logins.length <= 1 || disconnecting === login.provider"
                  :title="
                    logins.length <= 1
                      ? 'Cannot disconnect your only login method'
                      : `Disconnect ${providerLabel(login.provider)}`
                  "
                  @click="confirmDisconnect(login.provider)"
                >
                  {{ disconnecting === login.provider ? '…' : 'Disconnect' }}
                </button>
              </div>
            </div>
          </div>

          <div v-if="statusMsg" class="status-row" :class="statusClass">{{ statusMsg }}</div>
        </div>
      </div>

      <!-- Confirmation dialog -->
      <div v-if="confirmProvider" class="confirm-overlay">
        <div class="confirm-box">
          <div class="confirm-title">Disconnect {{ providerLabel(confirmProvider) }}?</div>
          <div class="confirm-body">
            This will remove your {{ providerLabel(confirmProvider) }} login. You will be signed
            out of this session.
          </div>
          <div class="confirm-actions">
            <button class="pixel-btn confirm-cancel" @click="confirmProvider = null">Cancel</button>
            <button class="pixel-btn confirm-ok" @click="doDisconnect">Disconnect</button>
          </div>
        </div>
      </div>

      <div class="modal-footer">
        <button class="pixel-btn" @click="$emit('close')">CLOSE</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useAuthStore } from '../stores/auth'
import { api } from '../utils/api'

defineEmits<{ close: [] }>()
const authStore = useAuthStore()

interface LoginEntry {
  provider: string
  username: string
  scope: string | null
  connected_at: string | null
  updated_at: string | null
}

const logins = ref<LoginEntry[]>([])
const loading = ref(true)
const disconnecting = ref<string | null>(null)
const confirmProvider = ref<string | null>(null)
const statusMsg = ref('')
const statusClass = ref<'status-ok' | 'status-err'>('status-ok')

let statusTimer: ReturnType<typeof setTimeout> | null = null

function providerLabel(p: string) {
  if (p === 'github') return 'GitHub'
  return p.charAt(0).toUpperCase() + p.slice(1)
}

async function fetchLogins() {
  loading.value = true
  try {
    const data = await api<{ logins: LoginEntry[] }>('/api/user/logins')
    logins.value = data.logins ?? []
  } catch {
    logins.value = []
  } finally {
    loading.value = false
  }
}

function setStatus(msg: string, ok: boolean) {
  statusMsg.value = msg
  statusClass.value = ok ? 'status-ok' : 'status-err'
  if (statusTimer) clearTimeout(statusTimer)
  statusTimer = setTimeout(() => {
    statusMsg.value = ''
  }, 3500)
}

function confirmDisconnect(provider: string) {
  confirmProvider.value = provider
}

async function doDisconnect() {
  const provider = confirmProvider.value
  confirmProvider.value = null
  if (!provider) return
  disconnecting.value = provider
  try {
    await api(`/api/user/logins/${provider}`, { method: 'DELETE' })
    setStatus(`${providerLabel(provider)} disconnected. You will be signed out.`, true)
    await authStore.logout()
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : 'Disconnect failed'
    setStatus(msg, false)
  } finally {
    disconnecting.value = null
  }
}

onMounted(fetchLogins)
</script>

<style scoped>
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.75);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 300;
}

.modal {
  background: var(--color-bg-secondary);
  border: 3px solid var(--color-brass);
  box-shadow: 0 0 40px rgba(232, 170, 0, 0.25);
  width: 480px;
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  position: relative;
}

.modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  background: var(--color-bg-tertiary);
  border-bottom: 2px solid var(--color-brass-dark);
  flex-shrink: 0;
}

.modal-title {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-brass-light);
  letter-spacing: 2px;
}

.close-btn {
  background: none;
  border: none;
  color: var(--color-text-dim);
  cursor: pointer;
  font-size: 14px;
  padding: 2px 6px;
  transition: color 0.15s;
}

.close-btn:hover {
  color: var(--color-text);
}

.modal-body {
  flex: 1;
  overflow-y: auto;
  padding: 18px 16px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.user-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px;
  background: rgba(0, 187, 170, 0.08);
  border: 1px solid rgba(0, 187, 170, 0.3);
  border-left: 3px solid var(--color-teal);
}

.avatar {
  width: 44px;
  height: 44px;
  border-radius: 50%;
  border: 2px solid var(--color-teal);
}

.user-info {
  flex: 1;
}

.user-name {
  font-family: var(--font-pixel);
  font-size: 8px;
  color: var(--color-teal);
  margin-bottom: 4px;
}

.user-label {
  font-size: 11px;
  color: var(--color-text-dim);
}

/* ── Connected Accounts section ── */

.section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.section-title {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-brass-dark);
  letter-spacing: 1.5px;
  text-transform: uppercase;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--color-brass-dark);
}

.loading-row,
.empty-row {
  font-size: 11px;
  color: var(--color-text-dim);
  padding: 8px 0;
}

.login-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.login-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 10px;
  background: var(--color-bg);
  border: 1px solid var(--color-brass-dark);
}

.provider-icon {
  font-size: 16px;
  color: var(--color-brass);
  line-height: 1;
}

.login-info {
  flex: 1;
  min-width: 0;
}

.login-provider {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-brass-light);
  letter-spacing: 1px;
  text-transform: uppercase;
  margin-bottom: 2px;
}

.login-username {
  font-size: 11px;
  color: var(--color-teal);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.disconnect-btn {
  flex-shrink: 0;
  background: none;
  border: 1px solid var(--color-red, #c0392b);
  color: var(--color-red, #c0392b);
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 1px;
  padding: 4px 8px;
  cursor: pointer;
  transition:
    background 0.12s,
    color 0.12s;
}

.disconnect-btn:hover:not(:disabled) {
  background: rgba(192, 57, 43, 0.15);
}

.disconnect-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
  border-color: var(--color-text-dim);
  color: var(--color-text-dim);
}

.status-row {
  font-family: var(--font-pixel);
  font-size: 7px;
  letter-spacing: 0.5px;
  padding-top: 4px;
}

.status-ok {
  color: var(--color-green, #27ae60);
}

.status-err {
  color: var(--color-red, #c0392b);
}

/* ── Confirmation dialog ── */

.confirm-overlay {
  position: absolute;
  inset: 0;
  background: rgba(0, 0, 0, 0.65);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 10;
}

.confirm-box {
  background: var(--color-bg-secondary);
  border: 2px solid var(--color-brass);
  padding: 20px;
  width: 320px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.confirm-title {
  font-family: var(--font-pixel);
  font-size: 8px;
  color: var(--color-brass-light);
  letter-spacing: 1.5px;
  text-transform: uppercase;
}

.confirm-body {
  font-size: 12px;
  color: var(--color-text-dim);
  line-height: 1.5;
}

.confirm-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.confirm-cancel {
  font-size: 7px;
  padding: 5px 10px;
}

.confirm-ok {
  font-size: 7px;
  padding: 5px 10px;
  border-color: var(--color-red, #c0392b);
  color: var(--color-red, #c0392b);
}

.confirm-ok:hover {
  background: rgba(192, 57, 43, 0.15);
}

.modal-footer {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
  padding: 12px 16px;
  border-top: 2px solid var(--color-brass-dark);
  background: var(--color-bg-tertiary);
  flex-shrink: 0;
}
</style>
