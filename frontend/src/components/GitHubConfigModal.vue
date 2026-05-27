<template>
  <div class="modal-overlay" @click.self="$emit('close')">
    <div class="modal">
      <div class="modal-header">
        <span class="modal-title">⚙ USER SETTINGS</span>
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
          <div class="section-title">API TOKENS</div>
          <div class="token-row">
            <div class="token-info">
              <span class="token-label">Claude</span>
              <span class="token-value" :class="{ unset: !tokens.claude }">
                {{ tokens.claude ?? 'Not set' }}
              </span>
            </div>
            <button
              class="pixel-btn danger-btn"
              :disabled="!tokens.claude || clearing === 'claude'"
              @click="clearToken('claude')"
            >
              {{ clearing === 'claude' ? '…' : 'CLEAR' }}
            </button>
          </div>
          <div class="token-row">
            <div class="token-info">
              <span class="token-label">Codex</span>
              <span class="token-value" :class="{ unset: !tokens.codex }">
                {{ tokens.codex ?? 'Not set' }}
              </span>
            </div>
            <button
              class="pixel-btn danger-btn"
              :disabled="!tokens.codex || clearing === 'codex'"
              @click="clearToken('codex')"
            >
              {{ clearing === 'codex' ? '…' : 'CLEAR' }}
            </button>
          </div>
          <div v-if="tokenError" class="token-error">{{ tokenError }}</div>
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

const tokens = ref<{ claude: string | null; codex: string | null }>({
  claude: null,
  codex: null,
})
const clearing = ref<'claude' | 'codex' | null>(null)
const tokenError = ref<string | null>(null)

async function loadTokens() {
  try {
    const data = await api<{ claude: string | null; codex: string | null }>(
      '/api/users/me/tokens',
    )
    tokens.value = data
  } catch {
    // Not fatal — just leave tokens as null
  }
}

async function clearToken(provider: 'claude' | 'codex') {
  clearing.value = provider
  tokenError.value = null
  try {
    await api(`/api/users/me/tokens/${provider}`, { method: 'DELETE' })
    tokens.value = { ...tokens.value, [provider]: null }
  } catch (err) {
    tokenError.value = err instanceof Error ? err.message : 'Failed to clear token'
  } finally {
    clearing.value = null
  }
}

onMounted(loadTokens)
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

.section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.section-title {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-brass-light);
  letter-spacing: 2px;
  padding-bottom: 4px;
  border-bottom: 1px solid var(--color-brass-dark);
}

.token-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 10px;
  background: var(--color-bg-tertiary);
  border: 1px solid rgba(255, 255, 255, 0.06);
}

.token-info {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.token-label {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-text-dim);
  letter-spacing: 1px;
}

.token-value {
  font-size: 11px;
  color: var(--color-text);
  font-family: monospace;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.token-value.unset {
  color: var(--color-text-dim);
  font-style: italic;
  font-family: inherit;
}

.danger-btn {
  flex-shrink: 0;
  border-color: rgba(255, 80, 80, 0.6) !important;
  color: rgba(255, 130, 130, 1) !important;
}

.danger-btn:not(:disabled):hover {
  background: rgba(255, 80, 80, 0.15) !important;
}

.danger-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.token-error {
  font-size: 11px;
  color: rgba(255, 130, 130, 1);
  padding: 4px 0;
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
