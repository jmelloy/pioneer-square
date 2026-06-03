<template>
  <teleport to="body">
    <div v-if="pending" class="auth-modal-overlay">
      <div class="auth-modal">
        <div class="auth-modal-header">
          <span class="auth-modal-title">⚿ CLAUDE AUTH REQUIRED</span>
          <span class="auth-modal-worker">{{
            agentsStore.workerDisplayName(pending.workerId)
          }}</span>
        </div>
        <div class="auth-modal-body">
          <p class="auth-instruction">
            Visit the URL below to authenticate Claude, then paste the code here.
          </p>
          <a :href="pending.url" target="_blank" rel="noopener" class="auth-url-block">
            {{ pending.url }}
          </a>
          <div class="auth-input-row">
            <input
              v-model="authCodeInput"
              class="auth-input"
              placeholder="Paste auth code here..."
              @keydown.enter="onSubmit"
              autofocus
            />
            <button
              class="pixel-btn auth-submit-btn"
              @click="onSubmit"
              :disabled="!authCodeInput.trim()"
            >
              ↵ Submit
            </button>
          </div>
        </div>
      </div>
    </div>
  </teleport>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useAgentsStore } from '../../stores/agents'

const agentsStore = useAgentsStore()

const props = defineProps<{
  pending: { workerId: string; url: string } | null
}>()

const emit = defineEmits<{
  (e: 'submit', payload: { workerId: string; code: string }): void
}>()

const authCodeInput = ref('')

function onSubmit() {
  if (!props.pending) return
  const code = authCodeInput.value.trim()
  if (!code) return
  emit('submit', { workerId: props.pending.workerId, code })
  authCodeInput.value = ''
}
</script>

<style scoped>
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
  box-shadow:
    0 0 40px rgba(192, 57, 43, 0.4),
    0 0 80px rgba(192, 57, 43, 0.15);
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
</style>
