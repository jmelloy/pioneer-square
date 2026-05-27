<template>
  <div class="landing">
    <LandingBackground />

    <div class="landing-content">
      <WorkshopPanel />

      <div class="main-panel">
        <div class="landing-header">
          <div class="logo-block">
            <div class="logo-title">PIONEER SQUARE</div>
            <div class="logo-subtitle">Multi-Agent Coding Workshop</div>
          </div>
          <div class="header-actions" v-if="authStore.isLoggedIn">
            <div class="user-pill">
              <img :src="authStore.user.avatar_url" class="user-avatar" alt="" />
              <span class="user-login">{{ authStore.user.login }}</span>
            </div>
            <button class="pixel-btn new-btn" @click="showNewModal = true">+ NEW GUILD</button>
            <button class="pixel-btn logout-btn" @click="handleLogout">Sign out</button>
          </div>
        </div>

        <LoginGate
          v-if="!authStore.isLoggedIn"
          :logging-in="loggingIn"
          :error="loginError"
          @login="handleLogin"
        />

        <div v-if="!authStore.isLoggedIn && isDev" class="dev-login-bar">
          <span class="dev-label">DEV MODE</span>
          <button class="pixel-btn dev-btn" :disabled="loggingIn" @click="handleDevLogin">
            SKIP LOGIN
          </button>
        </div>

        <template v-else>
          <div class="sessions-section">
            <div class="section-label">YOUR GUILDS</div>

            <div v-if="loading" class="loading-msg">Loading guilds...</div>

            <div v-else-if="guilds.length === 0" class="empty-state">
              <div class="empty-icon">⚙</div>
              <div class="empty-text">No guilds yet.</div>
              <div class="empty-sub">Start one to begin coordinating agents.</div>
              <button class="pixel-btn" @click="showNewModal = true">+ NEW GUILD</button>
            </div>

            <div v-else class="sessions-grid">
              <GuildCard
                v-for="guild in guilds"
                :key="guild.id"
                :guild="guild"
                @open="goToSession(guild.id)"
              />
            </div>
          </div>
        </template>
      </div>
    </div>

    <NewGuildModal
      v-if="showNewModal"
      :creating="creating"
      @close="showNewModal = false"
      @create="createGuild"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useGuildStore } from '../stores/guild'
import { useAuthStore } from '../stores/auth'
import { useGitHubStore } from '../stores/github'
import LandingBackground from '../components/landing/LandingBackground.vue'
import WorkshopPanel from '../components/landing/WorkshopPanel.vue'
import LoginGate from '../components/landing/LoginGate.vue'
import GuildCard from '../components/landing/GuildCard.vue'
import NewGuildModal from '../components/landing/NewGuildModal.vue'
import type { Guild } from '../types'

const router = useRouter()
const guildStore = useGuildStore()
const authStore = useAuthStore()
const ghStore = useGitHubStore()

const isDev = import.meta.env.DEV

const loading = ref(true)
const guilds = ref<Guild[]>([])
const showNewModal = ref(false)
const creating = ref(false)
const loggingIn = ref(false)
const loginError = ref('')

onMounted(async () => {
  const params = new URLSearchParams(window.location.search)
  if (params.has('code') && params.has('state')) {
    window.history.replaceState({}, '', '/')
    try {
      const data = await authStore.exchangeCode(params.get('code')!, params.get('state')!)
      if (data) ghStore.restoreGitHubToken(new URLSearchParams(data as Record<string, string>))
    } catch (e: unknown) {
      loginError.value = e instanceof Error ? e.message : String(e)
    }
  } else if (params.has('login_token')) {
    authStore.restoreFromCallback(params)
    ghStore.restoreGitHubToken(params)
    window.history.replaceState({}, '', '/')
  } else if (params.has('to')) {
    // Subdomain bridge: a guild subdomain redirected here to get the session token.
    const targetOrigin = params.get('to')!
    window.history.replaceState({}, '', '/')
    if (authStore.isLoggedIn && authStore.loginToken) {
      const bp = new URLSearchParams()
      bp.set('login_token', authStore.loginToken)
      if (authStore.user) {
        bp.set('gh_login', authStore.user.login)
        if (authStore.user.name) bp.set('gh_name', authStore.user.name)
        if (authStore.user.avatar_url) bp.set('gh_avatar', authStore.user.avatar_url)
        if (authStore.user.id) bp.set('gh_user_id', String(authStore.user.id))
      }
      window.location.href = `${targetOrigin}/?${bp}`
      return
    } else {
      // Not logged in — kick off OAuth and come back to the target subdomain.
      loggingIn.value = true
      try {
        await authStore.loginWithGitHub(targetOrigin)
      } catch (e: unknown) {
        loginError.value = e instanceof Error ? e.message : String(e)
        loggingIn.value = false
      }
      return
    }
  }

  if (authStore.isLoggedIn) {
    await guildStore.loadGuilds()
    guilds.value = guildStore.guilds
  }
  loading.value = false
})

function goToSession(id: string) {
  router.push(`/${id}`)
}

async function handleDevLogin() {
  loggingIn.value = true
  loginError.value = ''
  try {
    const guildId = await authStore.loginAsGuest()
    router.push(`/${guildId}`)
  } catch (e: unknown) {
    loginError.value = e instanceof Error ? e.message : String(e)
    loggingIn.value = false
  }
}

async function handleLogin() {
  loggingIn.value = true
  loginError.value = ''
  try {
    await authStore.loginWithGitHub()
  } catch (e: unknown) {
    loginError.value = e instanceof Error ? e.message : String(e)
    loggingIn.value = false
  }
}

async function handleLogout() {
  await authStore.logout()
  ghStore.logout()
  guilds.value = []
}

async function createGuild(name: string) {
  if (creating.value) return
  creating.value = true
  try {
    const guild = await guildStore.createGuild(name.trim() || undefined)
    router.push(`/${guild.id}`)
  } finally {
    creating.value = false
  }
}
</script>

<style scoped>
.landing {
  width: 100vw;
  height: 100dvh;
  background: var(--color-bg);
  display: flex;
  align-items: stretch;
  justify-content: center;
  position: relative;
  overflow: hidden;
}

.landing-content {
  position: relative;
  z-index: 1;
  width: 100%;
  max-width: 1200px;
  padding: 40px 32px;
  display: flex;
  flex-direction: row;
  gap: 40px;
  align-items: flex-start;
}

.main-panel {
  flex: 1;
  min-width: 0;
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
  text-shadow:
    0 0 20px rgba(255, 214, 68, 0.5),
    0 0 40px rgba(255, 214, 68, 0.2);
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

.user-pill {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 10px;
  background: var(--color-bg-tertiary);
  border: 1px solid var(--color-brass-dark);
}

.user-avatar {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  border: 1px solid var(--color-teal);
}

.user-login {
  font-size: 11px;
  color: var(--color-teal);
}

.logout-btn {
  font-size: 9px;
  padding: 6px 12px;
  background: transparent;
  border-color: var(--color-brass-dark);
  color: var(--color-text-dim);
}

.logout-btn:hover {
  border-color: var(--color-red);
  color: var(--color-red);
  box-shadow: none;
}

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

.dev-login-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 14px;
  border: 1px dashed rgba(0, 187, 170, 0.35);
  background: rgba(0, 187, 170, 0.04);
  margin-top: -20px;
}
.dev-label {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-teal);
  letter-spacing: 2px;
  opacity: 0.7;
}
.dev-btn {
  font-size: 8px;
  padding: 6px 14px;
  border-color: var(--color-teal);
  color: var(--color-teal);
  background: transparent;
  box-shadow: none;
}
.dev-btn:hover:not(:disabled) {
  background: rgba(0, 187, 170, 0.1);
  box-shadow: 0 0 8px rgba(0, 187, 170, 0.3);
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

@media (max-width: 900px) {
  .landing {
    overflow-y: auto;
    overflow-x: hidden;
  }

  .landing-content {
    flex-direction: column;
    padding: 24px 20px;
    gap: 24px;
    max-width: 100%;
  }
}

@media (max-width: 600px) {
  .landing-content {
    padding: 16px 12px;
    gap: 16px;
  }

  .landing-header {
    flex-direction: column;
    align-items: flex-start;
    gap: 12px;
    padding-bottom: 14px;
  }

  .logo-title {
    font-size: 13px;
    letter-spacing: 2px;
  }

  .logo-subtitle {
    font-size: 11px;
    letter-spacing: 1px;
  }

  .header-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    width: 100%;
  }

  .sessions-grid {
    grid-template-columns: 1fr;
  }
}
</style>
