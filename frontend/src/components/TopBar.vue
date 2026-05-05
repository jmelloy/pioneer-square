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
      :title="'GitHub settings for ' + authStore.user?.login"
      @click="showGitHubModal = true"
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
      @click="toggleSettings"
      title="Guild settings"
      ref="settingsBtnRef"
    >
      <span class="settings-icon">⚙</span>
    </button>

    <div v-if="showSettings && currentGuild" class="settings-popover" ref="popoverRef">
      <div class="settings-header">Guild Settings</div>

      <div class="settings-field">
        <label class="settings-label">Name</label>
        <div class="settings-row">
          <input
            v-model="renameValue"
            class="settings-input"
            @keydown.enter="commitRename"
            @keydown.escape="closeSettings"
          />
          <button
            class="pixel-btn settings-save-btn"
            :disabled="renameValue.trim() === (currentGuild.name || '') || !renameValue.trim()"
            @click="commitRename"
          >
            Save
          </button>
        </div>
        <span v-if="renameStatus" class="save-status" :class="'save-status-' + renameStatus">
          {{ renameStatus === 'saved' ? 'Saved' : 'Error' }}
        </span>
      </div>

      <div class="settings-field">
        <label class="settings-label">Primary Repo</label>
        <div class="settings-row">
          <select v-model="primaryRepoValue" class="settings-input" @change="savePrimaryRepo">
            <option value="">— select primary repo —</option>
            <option v-for="repo in ghStore.repos" :key="repo.full_name" :value="repo.full_name">
              {{ repo.full_name }}
            </option>
          </select>
          <span v-if="repoStatus" class="save-status" :class="'save-status-' + repoStatus">
            {{ repoStatus === 'saved' ? 'Saved' : 'Error' }}
          </span>
        </div>
      </div>

      <div class="settings-field settings-meta">
        <span class="settings-meta-label">Session ID</span>
        <code class="settings-meta-value">{{ currentGuild.id }}</code>
      </div>
    </div>
  </header>

  <GitHubConfigModal v-if="showGitHubModal" @close="showGitHubModal = false" />
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { useGuildStore } from '../stores/guild'
import { useGitHubStore } from '../stores/github'
import { useAuthStore } from '../stores/auth'
import GitHubConfigModal from './GitHubConfigModal.vue'

defineProps<{ debugActive?: boolean }>()
const emit = defineEmits<{ 'toggle-debug': [] }>()

const router = useRouter()
const guildStore = useGuildStore()
const ghStore = useGitHubStore()
const authStore = useAuthStore()

const currentGuild = computed(() => guildStore.currentGuild)

const showGitHubModal = ref(false)
const showSettings = ref(false)
const settingsBtnRef = ref<HTMLElement | null>(null)
const popoverRef = ref<HTMLElement | null>(null)

const renameValue = ref('')
const primaryRepoValue = ref('')
const renameStatus = ref<'' | 'saved' | 'error'>('')
const repoStatus = ref<'' | 'saved' | 'error'>('')
let renameStatusTimer: ReturnType<typeof setTimeout> | null = null
let repoStatusTimer: ReturnType<typeof setTimeout> | null = null

watch(
  currentGuild,
  (guild) => {
    renameValue.value = guild?.name ?? ''
    primaryRepoValue.value = guild?.primary_repo ?? ''
  },
  { immediate: true },
)

async function toggleSettings() {
  showSettings.value = !showSettings.value
  if (showSettings.value) {
    renameValue.value = currentGuild.value?.name ?? ''
    primaryRepoValue.value = currentGuild.value?.primary_repo ?? ''
    if (ghStore.repos.length === 0 && ghStore.token) {
      await ghStore.fetchRepos()
    }
  }
}

function closeSettings() {
  showSettings.value = false
}

function onDocClick(e: MouseEvent) {
  if (!showSettings.value) return
  const target = e.target as Node
  if (popoverRef.value?.contains(target)) return
  if (settingsBtnRef.value?.contains(target)) return
  showSettings.value = false
}

onMounted(() => {
  document.addEventListener('mousedown', onDocClick)
})

onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onDocClick)
  if (renameStatusTimer) clearTimeout(renameStatusTimer)
  if (repoStatusTimer) clearTimeout(repoStatusTimer)
})

async function commitRename() {
  if (!currentGuild.value) return
  const trimmed = renameValue.value.trim()
  if (!trimmed || trimmed === currentGuild.value.name) return
  try {
    await guildStore.renameGuild(currentGuild.value.id, trimmed)
    renameStatus.value = 'saved'
  } catch (e) {
    console.error('Failed to rename guild', e)
    renameStatus.value = 'error'
  } finally {
    if (renameStatusTimer) clearTimeout(renameStatusTimer)
    renameStatusTimer = setTimeout(() => {
      renameStatus.value = ''
    }, 2000)
  }
}

async function savePrimaryRepo() {
  if (!currentGuild.value) return
  try {
    await guildStore.updateGuild(currentGuild.value.id, {
      primary_repo: primaryRepoValue.value || null,
    })
    repoStatus.value = 'saved'
  } catch (e) {
    console.error('Failed to save primary repo', e)
    repoStatus.value = 'error'
  } finally {
    if (repoStatusTimer) clearTimeout(repoStatusTimer)
    repoStatusTimer = setTimeout(() => {
      repoStatus.value = ''
    }, 2000)
  }
  await nextTick()
}

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

.settings-popover {
  position: absolute;
  top: calc(100% + 4px);
  right: 12px;
  width: 320px;
  background: var(--color-bg-secondary);
  border: 2px solid var(--color-brass);
  box-shadow:
    0 6px 24px rgba(0, 0, 0, 0.5),
    0 0 16px rgba(232, 170, 0, 0.15);
  z-index: 250;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 14px;
}

.settings-header {
  font-family: var(--font-pixel);
  font-size: 8px;
  color: var(--color-brass-light);
  letter-spacing: 2px;
  text-transform: uppercase;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--color-brass-dark);
}

.settings-field {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.settings-label {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-brass-dark);
  letter-spacing: 1px;
  text-transform: uppercase;
}

.settings-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.settings-input {
  flex: 1;
  background: var(--color-bg);
  border: 1px solid var(--color-brass-dark);
  color: var(--color-text);
  font-family: var(--font-mono, monospace);
  font-size: 11px;
  padding: 5px 7px;
  outline: none;
  border-radius: 2px;
  min-width: 0;
}

.settings-input:focus {
  border-color: var(--color-brass);
}

.settings-save-btn {
  font-size: 7px;
  padding: 5px 9px;
  flex-shrink: 0;
}

.settings-save-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.save-status {
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 0.5px;
  white-space: nowrap;
  flex-shrink: 0;
}

.save-status-saved {
  color: var(--color-green);
}
.save-status-error {
  color: var(--color-red);
}

.settings-meta {
  flex-direction: row;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  border-top: 1px solid var(--color-brass-dark);
  padding-top: 10px;
}

.settings-meta-label {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-brass-dark);
  letter-spacing: 1px;
  text-transform: uppercase;
}

.settings-meta-value {
  font-family: var(--font-mono, monospace);
  font-size: 11px;
  color: var(--color-brass);
  background: var(--color-bg);
  border: 1px solid var(--color-brass-dark);
  padding: 2px 6px;
  border-radius: 2px;
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

  .settings-popover {
    right: 8px;
    width: calc(100vw - 16px);
    max-width: 320px;
  }
}
</style>
