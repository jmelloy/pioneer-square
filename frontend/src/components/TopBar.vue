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

      <div class="settings-field">
        <label class="settings-label">Claude Credentials</label>
        <div v-if="claudeCredsStatus === null" class="creds-loading">Loading…</div>
        <div v-else-if="claudeCredsStatus.saved" class="creds-saved-section">
          <div class="settings-row creds-saved-row">
            <span class="creds-saved-at"
              >Saved {{ formatCredsDate(claudeCredsStatus.updated_at) }}</span
            >
            <button
              v-if="!claudeCredsConfirmDelete"
              class="pixel-btn creds-delete-btn"
              :disabled="claudeCredsDeleting"
              @click="claudeCredsConfirmDelete = true"
            >
              Delete
            </button>
          </div>
          <div v-if="claudeCredsConfirmDelete" class="creds-confirm-row">
            <span class="creds-confirm-label">Delete credentials?</span>
            <button
              class="pixel-btn creds-confirm-yes-btn"
              :disabled="claudeCredsDeleting"
              @click="deleteClaude"
            >
              Confirm
            </button>
            <button
              class="pixel-btn creds-confirm-no-btn"
              @click="claudeCredsConfirmDelete = false"
            >
              Cancel
            </button>
          </div>
        </div>
        <div v-else class="creds-none">No credentials saved</div>
        <div v-if="claudeCredsError" class="creds-error">{{ claudeCredsError }}</div>
      </div>

      <div class="settings-field">
        <label class="settings-label">Foreman</label>
        <button
          class="pixel-btn foreman-toggle-btn"
          @click="showForemanConfig = !showForemanConfig"
        >
          {{ showForemanConfig ? 'Hide' : 'Configure' }}
        </button>
        <div v-if="showForemanConfig" class="foreman-config-section">
          <div class="foreman-field">
            <label class="foreman-field-label">Provider</label>
            <select v-model="foremanProvider" class="settings-input" @change="foremanModel = ''">
              <option value="">default (anthropic)</option>
              <option v-for="p in modelsStore.providers" :key="p.id" :value="p.id">
                {{ p.name }}
              </option>
            </select>
          </div>
          <div class="foreman-field">
            <label class="foreman-field-label">Model</label>
            <input
              v-model="foremanModel"
              class="settings-input"
              list="foreman-model-hints"
              placeholder="default"
              autocomplete="off"
            />
            <datalist id="foreman-model-hints">
              <option
                v-for="m in foremanProviderModels"
                :key="m.id"
                :value="m.id"
                :label="m.name"
              />
            </datalist>
          </div>

          <div class="foreman-field">
            <label class="foreman-field-label">
              {{ foremanProvider === 'bedrock' ? 'AWS Credentials' : 'Anthropic Credentials' }}
            </label>
            <div class="foreman-creds-grid">
              <div v-for="f in wellKnownFields" :key="f.key" class="foreman-cred-field">
                <label class="foreman-cred-label">{{ f.label }}</label>
                <input
                  class="settings-input"
                  type="text"
                  spellcheck="false"
                  autocomplete="off"
                  :placeholder="f.placeholder || ''"
                  :value="wellKnownValue(f.key)"
                  @input="setWellKnown(f.key, ($event.target as HTMLInputElement).value)"
                />
              </div>
            </div>
          </div>

          <div class="foreman-field">
            <label class="foreman-field-label">System Prompt Suffix</label>
            <textarea
              v-model="foremanSystemSuffix"
              class="settings-input foreman-textarea"
              placeholder="Additional instructions appended to the system prompt…"
              rows="3"
            />
          </div>
          <div class="foreman-field foreman-row">
            <div class="foreman-half">
              <label class="foreman-field-label">Max Rounds</label>
              <input
                v-model.number="foremanMaxRounds"
                class="settings-input"
                type="number"
                min="1"
                max="50"
                placeholder="10"
              />
            </div>
            <div class="foreman-half">
              <label class="foreman-field-label">Poll Min (s)</label>
              <input
                v-model.number="foremanPollMin"
                class="settings-input"
                type="number"
                min="10"
                placeholder="60"
              />
            </div>
            <div class="foreman-half">
              <label class="foreman-field-label">Poll Max (s)</label>
              <input
                v-model.number="foremanPollMax"
                class="settings-input"
                type="number"
                min="60"
                placeholder="3600"
              />
            </div>
          </div>

          <div class="foreman-field">
            <label class="foreman-field-label">Additional Environment Variables</label>
            <div class="env-var-list">
              <div v-for="row in additionalEnvVarRows" :key="row.id" class="env-var-row">
                <input
                  v-model="row.key"
                  class="settings-input env-var-key"
                  placeholder="KEY_NAME"
                  spellcheck="false"
                  autocomplete="off"
                />
                <input
                  v-model="row.value"
                  type="text"
                  class="settings-input env-var-value"
                  placeholder="value"
                  spellcheck="false"
                  autocomplete="off"
                />
                <button
                  class="env-var-delete-btn"
                  @click="removeEnvVar(row)"
                  title="Remove variable"
                >
                  ✕
                </button>
              </div>
              <button class="pixel-btn env-var-add-btn" @click="addEnvVar">+ Add Variable</button>
            </div>
          </div>

          <div class="foreman-actions">
            <button
              class="pixel-btn settings-save-btn"
              :disabled="foremanSaving"
              @click="saveForemanConfig"
            >
              {{ foremanSaving ? 'Saving…' : 'Save' }}
            </button>
            <span v-if="foremanStatus" class="save-status" :class="'save-status-' + foremanStatus">
              {{ foremanStatus === 'saved' ? 'Saved' : 'Error' }}
            </span>
          </div>
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
import { ref, computed, reactive, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { useGuildStore } from '../stores/guild'
import { useGitHubStore } from '../stores/github'
import { useAuthStore } from '../stores/auth'
import { useModels } from '../composables/useModels'
import GitHubConfigModal from './GitHubConfigModal.vue'

const API_BASE = (import.meta.env.VITE_API_BASE as string) ?? ''

defineProps<{ debugActive?: boolean }>()
const emit = defineEmits<{ 'toggle-debug': [] }>()

const router = useRouter()
const guildStore = useGuildStore()
const ghStore = useGitHubStore()
const authStore = useAuthStore()

const currentGuild = computed(() => guildStore.currentGuild)

const modelsStore = reactive(useModels())
const foremanProviderModels = computed(() =>
  foremanProvider.value ? modelsStore.modelsForProvider(foremanProvider.value) : [],
)
// Dedicated, provider-specific credential fields. These are stored as ordinary
// env_vars under the hood (the foreman client reads them via extra_env) but get
// first-class inputs so users don't have to remember exact var names.
interface WellKnownField {
  key: string
  label: string
  placeholder?: string
}
const FOREMAN_WELL_KNOWN: Record<string, WellKnownField[]> = {
  anthropic: [
    { key: 'ANTHROPIC_API_KEY', label: 'API Key', placeholder: 'sk-ant-…' },
    { key: 'ANTHROPIC_AUTH_TOKEN', label: 'Auth Token (optional)' },
    {
      key: 'ANTHROPIC_BASE_URL',
      label: 'Base URL (optional)',
      placeholder: 'https://api.anthropic.com',
    },
  ],
  bedrock: [
    { key: 'AWS_DEFAULT_REGION', label: 'Region', placeholder: 'us-east-1' },
    { key: 'AWS_PROFILE', label: 'Profile (optional)' },
    { key: 'AWS_ACCESS_KEY_ID', label: 'Access Key ID' },
    { key: 'AWS_SECRET_ACCESS_KEY', label: 'Secret Access Key' },
    { key: 'AWS_SESSION_TOKEN', label: 'Session Token (optional)' },
    { key: 'AWS_BEARER_TOKEN_BEDROCK', label: 'Bedrock Bearer Token (optional)' },
  ],
}

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

type CredsStatus = { saved: false } | { saved: true; updated_at: string | null }
const claudeCredsStatus = ref<CredsStatus | null>(null)
const claudeCredsDeleting = ref(false)
const claudeCredsError = ref<string | null>(null)
const claudeCredsConfirmDelete = ref(false)

const showForemanConfig = ref(false)
const foremanModel = ref('')
const foremanProvider = ref('')
const foremanSystemSuffix = ref('')
const foremanMaxRounds = ref<number | ''>('')
const foremanPollMin = ref<number | ''>('')
const foremanPollMax = ref<number | ''>('')
const foremanSaving = ref(false)
const foremanStatus = ref<'' | 'saved' | 'error'>('')
let foremanStatusTimer: ReturnType<typeof setTimeout> | null = null
const wellKnownFields = computed(
  () => FOREMAN_WELL_KNOWN[foremanProvider.value || 'anthropic'] ?? [],
)
const wellKnownKeys = computed(() => new Set(wellKnownFields.value.map((f) => f.key)))
// Free-form list excludes keys that have a dedicated field for the current provider.
const additionalEnvVarRows = computed(() =>
  envVarRows.value.filter((r) => !wellKnownKeys.value.has(r.key)),
)

interface EnvVarRow {
  id: number
  key: string
  value: string
}
let envRowSeq = 0
const envVarRows = ref<EnvVarRow[]>([])

function wellKnownValue(key: string): string {
  return envVarRows.value.find((r) => r.key === key)?.value ?? ''
}

function setWellKnown(key: string, value: string) {
  const row = envVarRows.value.find((r) => r.key === key)
  if (value) {
    if (row) row.value = value
    else envVarRows.value.push({ id: ++envRowSeq, key, value })
  } else if (row) {
    // Empty value → drop the row so we don't persist blank vars.
    envVarRows.value.splice(envVarRows.value.indexOf(row), 1)
  }
}

async function loadForemanConfig() {
  if (!currentGuild.value) return
  try {
    const res = await fetch(
      `${API_BASE}/api/guilds/${encodeURIComponent(currentGuild.value.id)}/foreman-config`,
      { headers: authStore.authHeaders() },
    )
    if (res.ok) {
      const cfg = await res.json()
      foremanModel.value = cfg.model ?? ''
      foremanProvider.value = cfg.provider ?? ''
      foremanSystemSuffix.value = cfg.system_prompt_suffix ?? ''
      foremanMaxRounds.value = cfg.max_rounds ?? ''
      foremanPollMin.value = cfg.poll_min_interval ?? ''
      foremanPollMax.value = cfg.poll_max_interval ?? ''
      // Env var values are returned in clear text so they can be verified/edited.
      envVarRows.value = (cfg.env_vars ?? []).map((e: { key: string; value?: string }) => ({
        id: ++envRowSeq,
        key: e.key,
        value: e.value ?? '',
      }))
    }
  } catch {
    // non-fatal: fields stay blank (will use server defaults)
  }
}

function addEnvVar() {
  envVarRows.value.push({ id: ++envRowSeq, key: '', value: '' })
}

function removeEnvVar(row: EnvVarRow) {
  const i = envVarRows.value.indexOf(row)
  if (i >= 0) envVarRows.value.splice(i, 1)
}

async function saveForemanConfig() {
  if (!currentGuild.value) return
  foremanSaving.value = true
  foremanStatus.value = ''
  try {
    const body: Record<string, unknown> = {}
    if (foremanModel.value) body.model = foremanModel.value
    else body.model = null
    if (foremanProvider.value) body.provider = foremanProvider.value
    else body.provider = null
    if (foremanSystemSuffix.value) body.system_prompt_suffix = foremanSystemSuffix.value
    else body.system_prompt_suffix = null
    if (foremanMaxRounds.value !== '') body.max_rounds = foremanMaxRounds.value
    else body.max_rounds = null
    if (foremanPollMin.value !== '') body.poll_min_interval = foremanPollMin.value
    else body.poll_min_interval = null
    if (foremanPollMax.value !== '') body.poll_max_interval = foremanPollMax.value
    else body.poll_max_interval = null
    // Send actual values for every keyed row (dedicated credential fields and
    // free-form rows alike both live in envVarRows). Skip rows with empty keys.
    body.env_vars = envVarRows.value
      .filter((r) => r.key.trim())
      .map((r) => ({ key: r.key.trim(), value: r.value }))
    const res = await fetch(
      `${API_BASE}/api/guilds/${encodeURIComponent(currentGuild.value.id)}/foreman-config`,
      {
        method: 'PATCH',
        headers: { ...authStore.authHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      },
    )
    if (res.ok) {
      foremanStatus.value = 'saved'
      // Re-sync with the server's canonical config (clear-text values).
      const saved = await res.json()
      envVarRows.value = (saved.env_vars ?? []).map((e: { key: string; value?: string }) => ({
        id: ++envRowSeq,
        key: e.key,
        value: e.value ?? '',
      }))
    } else {
      foremanStatus.value = 'error'
    }
  } catch {
    foremanStatus.value = 'error'
  } finally {
    foremanSaving.value = false
    if (foremanStatusTimer) clearTimeout(foremanStatusTimer)
    foremanStatusTimer = setTimeout(() => {
      foremanStatus.value = ''
    }, 2000)
  }
}

async function loadClaudeCredsStatus() {
  if (!currentGuild.value) return
  claudeCredsStatus.value = null
  claudeCredsError.value = null
  try {
    const res = await fetch(
      `${API_BASE}/auth/claude-credentials?guild_id=${encodeURIComponent(currentGuild.value.id)}`,
      { headers: authStore.authHeaders() },
    )
    if (res.ok) {
      claudeCredsStatus.value = await res.json()
    } else {
      claudeCredsError.value = `Failed to load credentials status (${res.status})`
      claudeCredsStatus.value = { saved: false }
    }
  } catch {
    claudeCredsError.value = 'Failed to load credentials status'
    claudeCredsStatus.value = { saved: false }
  }
}

async function deleteClaude() {
  if (!currentGuild.value) return
  claudeCredsDeleting.value = true
  claudeCredsError.value = null
  try {
    const res = await fetch(
      `${API_BASE}/auth/claude-credentials?guild_id=${encodeURIComponent(currentGuild.value.id)}`,
      { method: 'DELETE', headers: authStore.authHeaders() },
    )
    if (res.ok) {
      claudeCredsStatus.value = { saved: false }
      claudeCredsConfirmDelete.value = false
    } else {
      claudeCredsError.value = `Delete failed (${res.status})`
    }
  } catch {
    claudeCredsError.value = 'Delete failed'
  } finally {
    claudeCredsDeleting.value = false
  }
}

function formatCredsDate(iso: string | null) {
  if (!iso) return 'unknown date'
  return new Date(iso).toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  })
}

watch(
  currentGuild,
  (guild) => {
    renameValue.value = guild?.name ?? ''
    primaryRepoValue.value = guild?.primary_repo ?? ''
    claudeCredsStatus.value = null
    claudeCredsError.value = null
    claudeCredsConfirmDelete.value = false
    showForemanConfig.value = false
    foremanModel.value = ''
    foremanProvider.value = ''
    foremanSystemSuffix.value = ''
    foremanMaxRounds.value = ''
    foremanPollMin.value = ''
    foremanPollMax.value = ''
    foremanStatus.value = ''
    envVarRows.value = []
  },
  { immediate: true },
)

watch(showForemanConfig, (open) => {
  if (open) {
    loadForemanConfig()
    modelsStore.loadModels()
  }
})

watch(showSettings, (open) => {
  if (!open) {
    claudeCredsConfirmDelete.value = false
  }
})

async function toggleSettings() {
  showSettings.value = !showSettings.value
  if (showSettings.value) {
    renameValue.value = currentGuild.value?.name ?? ''
    primaryRepoValue.value = currentGuild.value?.primary_repo ?? ''
    if (ghStore.repos.length === 0 && ghStore.token) {
      await ghStore.fetchRepos()
    }
    await loadClaudeCredsStatus()
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
  if (foremanStatusTimer) clearTimeout(foremanStatusTimer)
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

.creds-loading {
  font-family: var(--font-mono, monospace);
  font-size: 10px;
  color: var(--color-text-dim);
}

.creds-saved-row {
  align-items: center;
  justify-content: space-between;
}

.creds-saved-at {
  font-family: var(--font-mono, monospace);
  font-size: 11px;
  color: var(--color-text);
}

.creds-delete-btn {
  font-size: 7px;
  padding: 5px 9px;
  flex-shrink: 0;
  border-color: var(--color-red, #c0392b);
  color: var(--color-red, #e74c3c);
}

.creds-delete-btn:hover:not(:disabled) {
  background: rgba(192, 57, 43, 0.2);
}

.creds-delete-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.creds-saved-section {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.creds-confirm-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.creds-confirm-label {
  font-family: var(--font-mono, monospace);
  font-size: 10px;
  color: var(--color-red, #e74c3c);
  flex: 1;
  white-space: nowrap;
}

.creds-confirm-yes-btn {
  font-size: 7px;
  padding: 5px 9px;
  flex-shrink: 0;
  border-color: var(--color-red, #c0392b);
  color: var(--color-red, #e74c3c);
}

.creds-confirm-yes-btn:hover:not(:disabled) {
  background: rgba(192, 57, 43, 0.2);
}

.creds-confirm-yes-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.creds-confirm-no-btn {
  font-size: 7px;
  padding: 5px 9px;
  flex-shrink: 0;
}

.creds-none {
  font-family: var(--font-mono, monospace);
  font-size: 11px;
  color: var(--color-text-dim);
  font-style: italic;
}

.creds-error {
  font-family: var(--font-mono, monospace);
  font-size: 10px;
  color: var(--color-red, #e74c3c);
}

.foreman-toggle-btn {
  font-size: 7px;
  padding: 5px 9px;
  align-self: flex-start;
}

.foreman-config-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 6px;
  padding: 10px;
  background: var(--color-bg);
  border: 1px solid var(--color-brass-dark);
  border-radius: 2px;
}

.foreman-field {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.foreman-field-label {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-brass-dark);
  letter-spacing: 1px;
  text-transform: uppercase;
}

.foreman-textarea {
  resize: vertical;
  min-height: 60px;
  font-family: var(--font-mono, monospace);
  font-size: 10px;
}

.foreman-row {
  flex-direction: row;
  gap: 8px;
  align-items: flex-start;
}

.foreman-half {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.foreman-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 2px;
}

.env-var-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.env-var-row {
  display: flex;
  align-items: center;
  gap: 4px;
}

.env-var-key {
  flex: 0 0 130px;
  min-width: 0;
  font-size: 10px;
}

.env-var-value {
  flex: 1;
  min-width: 0;
  font-size: 10px;
}

.foreman-creds-grid {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.foreman-cred-field {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.foreman-cred-label {
  font-family: var(--font-mono, monospace);
  font-size: 9px;
  color: var(--color-text-dim);
  letter-spacing: 0.5px;
}

.env-var-delete-btn {
  background: none;
  border: 1px solid var(--color-brass-dark);
  color: var(--color-text-dim);
  cursor: pointer;
  width: 22px;
  height: 22px;
  border-radius: 2px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  flex-shrink: 0;
  transition:
    border-color 0.12s,
    color 0.12s;
}

.env-var-delete-btn:hover {
  border-color: var(--color-red, #c0392b);
  color: var(--color-red, #c0392b);
}

.env-var-add-btn {
  font-size: 7px;
  padding: 4px 8px;
  align-self: flex-start;
  margin-top: 2px;
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
