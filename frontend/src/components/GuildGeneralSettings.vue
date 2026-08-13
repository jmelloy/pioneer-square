<template>
  <div class="settings-field">
    <label class="settings-label">Name</label>
    <div class="settings-row">
      <input v-model="renameValue" class="settings-input" @keydown.enter="commitRename" />
      <button
        class="pixel-btn settings-save-btn"
        :disabled="renameValue.trim() === (currentGuild?.name || '') || !renameValue.trim()"
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

  <div class="settings-field">
    <label class="settings-label">GitHub App Installation ID</label>
    <p class="settings-hint">
      From github.com/settings/installations/&lt;id&gt;. Attributes comments and commits to the
      app bot. Blank uses the server default.
    </p>
    <div class="settings-row">
      <input
        v-model="githubAppInstallationId"
        type="text"
        inputmode="numeric"
        placeholder="e.g. 149025614"
        class="settings-input"
        @keydown.enter="saveGithubAppInstallation"
      />
      <button
        class="pixel-btn settings-save-btn"
        :disabled="appInstallSaving || githubAppInstallationId.trim() === savedInstallId"
        @click="saveGithubAppInstallation"
      >
        {{ appInstallSaving ? 'Saving…' : 'Save' }}
      </button>
      <span v-if="appInstallStatus" class="save-status" :class="'save-status-' + appInstallStatus">
        {{ appInstallStatus === 'saved' ? 'Saved' : 'Error' }}
      </span>
    </div>
  </div>

  <div class="settings-field settings-meta">
    <span class="settings-meta-label">Session ID</span>
    <code class="settings-meta-value">{{ currentGuild?.id }}</code>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useGuildStore } from '../stores/guild'
import { useGitHubStore } from '../stores/github'
import { useAuthStore } from '../stores/auth'

const API_BASE = (import.meta.env.VITE_API_BASE as string) ?? ''

const guildStore = useGuildStore()
const ghStore = useGitHubStore()
const authStore = useAuthStore()

const currentGuild = computed(() => guildStore.currentGuild)

const renameValue = ref('')
const primaryRepoValue = ref('')
const renameStatus = ref<'' | 'saved' | 'error'>('')
const repoStatus = ref<'' | 'saved' | 'error'>('')
let renameStatusTimer: ReturnType<typeof setTimeout> | null = null
let repoStatusTimer: ReturnType<typeof setTimeout> | null = null

const githubAppInstallationId = ref('')
const savedInstallId = ref('') // baseline for the "unchanged" disabled check
const appInstallStatus = ref<'' | 'saved' | 'error'>('')
const appInstallSaving = ref(false)
let appInstallStatusTimer: ReturnType<typeof setTimeout> | null = null

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
}

async function saveGithubAppInstallation() {
  if (!currentGuild.value) return
  appInstallSaving.value = true
  appInstallStatus.value = ''
  try {
    const res = await fetch(
      `${API_BASE}/guilds/${encodeURIComponent(currentGuild.value.id)}/github-app-installation`,
      {
        method: 'PUT',
        headers: { ...authStore.authHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ installation_id: githubAppInstallationId.value.trim() }),
      },
    )
    if (res.ok) {
      const saved = await res.json()
      githubAppInstallationId.value = saved.github_app_installation_id ?? ''
      savedInstallId.value = githubAppInstallationId.value
      appInstallStatus.value = 'saved'
    } else {
      appInstallStatus.value = 'error'
    }
  } catch {
    appInstallStatus.value = 'error'
  } finally {
    appInstallSaving.value = false
    if (appInstallStatusTimer) clearTimeout(appInstallStatusTimer)
    appInstallStatusTimer = setTimeout(() => {
      appInstallStatus.value = ''
    }, 2000)
  }
}

onMounted(async () => {
  renameValue.value = currentGuild.value?.name ?? ''
  primaryRepoValue.value = currentGuild.value?.primary_repo ?? ''
  githubAppInstallationId.value = currentGuild.value?.github_app_installation_id ?? ''
  savedInstallId.value = githubAppInstallationId.value
  if (ghStore.repos.length === 0 && ghStore.token) {
    await ghStore.fetchRepos()
  }
})

onBeforeUnmount(() => {
  if (renameStatusTimer) clearTimeout(renameStatusTimer)
  if (repoStatusTimer) clearTimeout(repoStatusTimer)
  if (appInstallStatusTimer) clearTimeout(appInstallStatusTimer)
})
</script>

<style scoped>
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

.settings-hint {
  font-family: var(--font-mono, monospace);
  font-size: 9px;
  color: var(--color-text-dim);
  font-style: italic;
  margin: 0 0 3px;
  line-height: 1.4;
}

.settings-meta {
  flex-direction: row;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  border-top: 1px solid var(--color-brass-dark);
  padding-top: 12px;
  margin-top: 4px;
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
</style>
