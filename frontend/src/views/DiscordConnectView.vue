<template>
  <div class="discord-connect ui-surface">
    <div class="connect-card">
      <div class="connect-icon" aria-hidden="true">⚙</div>
      <h1 class="connect-title">CONNECT DISCORD ACCOUNT</h1>

      <div v-if="status === 'redirecting'" class="connect-sub" role="status">
        Redirecting you to sign in to Pioneer Square...
      </div>
      <div v-else-if="status === 'linking'" class="connect-sub" role="status">
        Linking your account...
      </div>
      <div v-else-if="status === 'success'" class="connect-success" role="status">
        ✅ Your Discord account (<strong>{{ discordUsername }}</strong
        >) is now linked to Pioneer Square.
      </div>
      <div v-else class="connect-error" role="alert">{{ error }}</div>

      <button class="pixel-btn connect-btn" @click="router.push('/')">Go to Pioneer Square</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { api, ApiError } from '../utils/api'

const PENDING_TOKEN_KEY = 'discord_connect_pending_token'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()

const status = ref<'redirecting' | 'linking' | 'success' | 'error'>('linking')
const error = ref('')
const discordUsername = ref('')

onMounted(async () => {
  const token = (route.query.token as string) || ''
  if (!token) {
    status.value = 'error'
    error.value = 'Missing connect token — use the link Discord gave you.'
    return
  }

  if (!authStore.isLoggedIn) {
    status.value = 'redirecting'
    localStorage.setItem(PENDING_TOKEN_KEY, token)
    try {
      await authStore.loginWithGitHub()
    } catch (e: unknown) {
      status.value = 'error'
      error.value = e instanceof Error ? e.message : String(e)
    }
    return
  }

  status.value = 'linking'
  try {
    const data = await api<{ discord_username: string }>('/api/discord/connect', {
      method: 'POST',
      json: { token },
    })
    discordUsername.value = data.discord_username
    status.value = 'success'
  } catch (e: unknown) {
    status.value = 'error'
    error.value = e instanceof ApiError ? e.message : 'Failed to link your Discord account.'
  }
})
</script>

<style scoped>
.discord-connect {
  width: 100vw;
  height: 100dvh;
  background: var(--color-bg);
  display: flex;
  align-items: flex-start;
  justify-content: flex-start;
  padding: 16px;
  overflow-y: auto;
}

.connect-card {
  margin: auto;
  flex-shrink: 0;
  overflow-wrap: anywhere;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
  padding: clamp(20px, 5vw, 48px) clamp(16px, 5vw, 40px);
  background: var(--color-bg-secondary);
  border: 2px solid var(--color-brass-dark);
  box-shadow: 0 0 32px rgba(232, 170, 0, 0.1);
  max-width: 440px;
  width: 100%;
  text-align: center;
}

.connect-icon {
  font-size: 48px;
  opacity: 0.4;
  animation: spin 8s linear infinite;
}

.connect-title {
  font: 400 var(--text-page-title) / var(--leading-heading) var(--font-display);
  color: var(--color-brass-light);
}

.connect-sub {
  font-size: var(--text-body);
  color: var(--color-text-dim);
  line-height: 1.6;
}

.connect-success {
  font-size: var(--text-body);
  color: var(--color-teal);
  line-height: 1.6;
}

.connect-error {
  font-size: var(--text-body);
  color: var(--color-red);
  padding: 6px 10px;
  background: var(--color-bg);
  border: 1px solid rgba(238, 51, 34, 0.3);
  width: 100%;
}

.connect-btn {
  padding: 12px 24px;
  margin-top: 8px;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
@media (prefers-reduced-motion: reduce) {
  .connect-icon {
    animation: none;
  }
}
</style>
