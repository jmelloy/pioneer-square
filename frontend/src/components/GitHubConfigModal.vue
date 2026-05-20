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
      </div>

      <div class="modal-footer">
        <button class="pixel-btn" @click="$emit('close')">CLOSE</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useAuthStore } from '../stores/auth'

defineEmits<{ close: [] }>()
const authStore = useAuthStore()
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
