<template>
  <header class="top-bar">
    <button class="home-btn pixel-btn" @click="goHome" title="Back to guilds">
      <span class="home-icon">⌂</span>
      <span class="home-label">Home</span>
    </button>

    <div class="guild-block" v-if="currentGuild">
      <span class="guild-name" :title="currentGuild.name">{{
        currentGuild.name || 'Unnamed Guild'
      }}</span>
      <span class="guild-id">#{{ currentGuild.id }}</span>
    </div>
    <div class="guild-block guild-block--empty" v-else>
      <span class="guild-name">Pioneer Square</span>
    </div>

    <div class="top-bar-spacer"></div>

    <button
      v-if="authStore.isLoggedIn"
      class="user-pill"
      :title="'Preferences for ' + authStore.user?.login"
      @click="showUserPreferences = true"
    >
      <img
        v-if="authStore.user?.avatar_url"
        :src="authStore.user.avatar_url"
        class="user-avatar"
        alt=""
      />
      <span class="user-login">{{ authStore.user?.login }}</span>
    </button>

    <button
      v-if="currentGuild"
      class="debug-btn"
      :class="{ active: debugActive }"
      @click="emit('toggle-debug')"
      title="Debug: foreman context"
    >
      <span class="debug-icon">⌥</span>
    </button>

    <button
      v-if="currentGuild"
      class="settings-btn"
      :class="{ active: showSettings }"
      @click="showSettings = !showSettings"
      title="Guild settings"
    >
      <span class="settings-icon">⚙</span>
    </button>
  </header>

  <GuildSettingsPanel v-if="showSettings && currentGuild" @close="showSettings = false" />
  <UserPreferencesModal v-if="showUserPreferences" @close="showUserPreferences = false" />
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { useGuildStore } from '../stores/guild'
import { useAuthStore } from '../stores/auth'
import UserPreferencesModal from './UserPreferencesModal.vue'
import GuildSettingsPanel from './GuildSettingsPanel.vue'

defineProps<{ debugActive?: boolean }>()
const emit = defineEmits<{ 'toggle-debug': [] }>()

const router = useRouter()
const guildStore = useGuildStore()
const authStore = useAuthStore()

const currentGuild = computed(() => guildStore.currentGuild)

const showUserPreferences = ref(false)
const showSettings = ref(false)

function goHome() {
  router.push('/')
}
</script>

<style scoped>
.top-bar {
  position: relative;
  display: flex;
  align-items: center;
  gap: 12px;
  height: 44px;
  padding: 0 12px;
  background: var(--color-bg-tertiary);
  border-bottom: 2px solid var(--color-brass-dark);
  flex-shrink: 0;
  z-index: 100;
}

.home-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 7px;
  padding: 6px 10px;
}

.home-icon {
  font-size: 12px;
  line-height: 1;
}

.home-label {
  letter-spacing: 1px;
}

.guild-block {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
  overflow: hidden;
  padding-left: 4px;
  border-left: 1px solid var(--color-brass-dark);
  padding-right: 4px;
}

.guild-block--empty {
  border-left: none;
}

.guild-name {
  font-family: var(--font-pixel);
  font-size: 9px;
  color: var(--color-brass-light);
  letter-spacing: 1.5px;
  text-transform: uppercase;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  text-shadow: 0 0 6px rgba(255, 214, 68, 0.3);
}

.guild-id {
  font-family: var(--font-mono, monospace);
  font-size: 10px;
  color: var(--color-text-dim);
  white-space: nowrap;
  flex-shrink: 0;
}

.top-bar-spacer {
  flex: 1;
  min-width: 0;
}

.user-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 8px 3px 4px;
  background: var(--color-bg-secondary);
  border: 1px solid var(--color-brass-dark);
  border-radius: 2px;
  flex-shrink: 0;
  cursor: pointer;
  transition:
    border-color 0.12s,
    background 0.12s;
}

.user-pill:hover {
  border-color: var(--color-brass);
  background: rgba(232, 170, 0, 0.06);
}

.user-avatar {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  border: 1px solid var(--color-teal);
}

.user-login {
  font-size: 11px;
  color: var(--color-teal);
  white-space: nowrap;
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.debug-btn {
  background: var(--color-bg-secondary);
  border: 1px solid var(--color-brass-dark);
  color: var(--color-text-dim);
  cursor: pointer;
  width: 30px;
  height: 30px;
  border-radius: 2px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  flex-shrink: 0;
  transition:
    border-color 0.12s,
    background 0.12s,
    color 0.12s;
}

.debug-btn:hover,
.debug-btn.active {
  border-color: var(--color-teal);
  background: rgba(0, 187, 170, 0.1);
  color: var(--color-teal);
}

.debug-icon {
  font-size: 14px;
  line-height: 1;
}

.settings-btn {
  background: var(--color-bg-secondary);
  border: 1px solid var(--color-brass-dark);
  color: var(--color-brass);
  cursor: pointer;
  width: 30px;
  height: 30px;
  border-radius: 2px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  flex-shrink: 0;
  transition:
    border-color 0.12s,
    background 0.12s,
    color 0.12s;
}

.settings-btn:hover,
.settings-btn.active {
  border-color: var(--color-brass);
  background: rgba(232, 170, 0, 0.1);
  color: var(--color-brass-light);
}

.settings-icon {
  font-size: 16px;
  line-height: 1;
}

@media (max-width: 600px) {
  .home-label {
    display: none;
  }

  .guild-id {
    display: none;
  }

  .user-login {
    display: none;
  }
}
</style>
