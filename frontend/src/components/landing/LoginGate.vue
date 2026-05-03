<template>
  <div class="login-gate">
    <div class="login-card">
      <div class="login-icon">⚙</div>
      <div class="login-title">SIGN IN TO GET STARTED</div>
      <div class="login-sub">Connect your GitHub account to create guilds and run agents.</div>
      <button class="pixel-btn login-btn" :disabled="loggingIn" @click="$emit('login')">
        {{ loggingIn ? 'Redirecting...' : 'SIGN IN WITH GITHUB' }}
      </button>
      <div v-if="error" class="login-error">{{ error }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
defineProps<{ loggingIn: boolean; error: string }>()
defineEmits<{ (e: 'login'): void }>()
</script>

<style scoped>
.login-gate {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 40px 0;
}

.login-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
  padding: 48px 40px;
  background: var(--color-bg-secondary);
  border: 2px solid var(--color-brass-dark);
  box-shadow: 0 0 32px rgba(232, 170, 0, 0.1);
  max-width: 400px;
  width: 100%;
  text-align: center;
}

.login-icon {
  font-size: 48px;
  opacity: 0.4;
  animation: spin 8s linear infinite;
}

.login-title {
  font-family: var(--font-pixel);
  font-size: 9px;
  color: var(--color-brass-light);
  letter-spacing: 2px;
}

.login-sub {
  font-size: 12px;
  color: var(--color-text-dim);
  line-height: 1.6;
}

.login-btn {
  font-size: 9px;
  padding: 12px 24px;
  margin-top: 8px;
}

.login-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
  pointer-events: none;
}

.login-error {
  font-size: 11px;
  color: var(--color-red);
  padding: 6px 10px;
  background: rgba(238, 51, 34, 0.1);
  border: 1px solid rgba(238, 51, 34, 0.3);
  width: 100%;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

@media (max-width: 600px) {
  .login-gate {
    padding: 20px 0;
  }
  .login-card {
    padding: 28px 16px;
  }
}
</style>
