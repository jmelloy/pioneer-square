<template>
  <div class="spawn-form">
    <div v-if="launched" class="spawn-success">
      <span class="spawn-success-label">Launched</span>
      <span class="spawn-success-id">{{ launchedWorkerId }}</span>
    </div>
    <template v-else>
      <label class="spawn-label">Repos</label>
      <div v-if="loadingRepos" class="spawn-hint-text">Loading repos…</div>
      <div v-else-if="repoFetchFailed" class="spawn-error">
        Failed to load repos — saved selection cleared.
      </div>
      <div v-else-if="ghStore.repos.length === 0" class="spawn-hint-text">
        No repos found — configure GitHub first.
      </div>
      <div v-else class="spawn-repo-list">
        <template v-for="group in groupedRepos" :key="group.owner">
          <label
            class="spawn-repo-row spawn-org-row"
            :class="{ selected: orgAllSelected(group.owner) }"
          >
            <input
              type="checkbox"
              :ref="(el) => setOrgCheckboxRef(el as HTMLInputElement | null, group.owner)"
              :checked="orgAllSelected(group.owner)"
              @change="toggleOrg(group.owner)"
              class="spawn-repo-check"
            />
            <span class="spawn-repo-name spawn-org-name">{{ group.owner }}</span>
          </label>
          <label
            v-for="repo in group.repos"
            :key="repo.full_name"
            class="spawn-repo-row spawn-repo-indent"
            :class="{ selected: selectedRepos.includes(repo.full_name) }"
          >
            <input
              type="checkbox"
              :checked="selectedRepos.includes(repo.full_name)"
              @change="toggleRepo(repo.full_name)"
              class="spawn-repo-check"
            />
            <span class="spawn-repo-name">{{ repo.full_name.split('/')[1] }}</span>
          </label>
        </template>
      </div>
      <div v-if="hasGuildDefaults" class="spawn-defaults-row">
        <span class="spawn-defaults-note">
          {{ usingGuildDefaults ? 'Prefilled from guild defaults' : 'Overriding guild defaults' }}
        </span>
        <button
          v-if="!usingGuildDefaults"
          class="spawn-defaults-reset"
          type="button"
          @click="resetToGuildDefaults"
        >
          Reset to guild defaults
        </button>
      </div>
      <label class="spawn-label">Name <span class="spawn-hint">(optional)</span></label>
      <input v-model="name" class="spawn-input" type="text" placeholder="auto-generated" />
      <label class="spawn-label">Tools <span class="spawn-hint">(optional)</span></label>
      <div class="spawn-tool-list">
        <label
          v-for="tool in AVAILABLE_TOOLS"
          :key="tool"
          class="spawn-repo-row"
          :class="{ selected: selectedTools.includes(tool) }"
        >
          <input
            type="checkbox"
            :checked="selectedTools.includes(tool)"
            @change="toggleTool(tool)"
            class="spawn-repo-check"
          />
          <span class="spawn-repo-name">{{ tool }}</span>
        </label>
      </div>
      <label class="spawn-label">Agents <span class="spawn-hint">(optional)</span></label>
      <input
        v-model.number="agentCount"
        class="spawn-input spawn-input--narrow"
        type="number"
        min="1"
        max="16"
        placeholder="4"
      />
      <label class="spawn-label">Guild Credentials <span class="spawn-hint">(optional)</span></label>
      <div v-if="credentialsLoading" class="spawn-hint-text">Loading credentials…</div>
      <div v-else-if="credentialsError" class="spawn-error">{{ credentialsError }}</div>
      <div v-else-if="!hasCredentials" class="spawn-hint-text">No guild credentials configured.</div>
      <template v-else>
        <div class="spawn-cred-list">
          <label
            v-for="cred in credentials!.guild_env_vars"
            :key="cred.key"
            class="spawn-repo-row spawn-cred-row"
            :class="{ selected: includedKeys[cred.key] !== false }"
          >
            <input
              type="checkbox"
              :checked="includedKeys[cred.key] !== false"
              @change="toggleCredential(cred.key)"
              class="spawn-repo-check"
            />
            <span class="spawn-repo-name spawn-cred-key">{{ cred.key }}</span>
            <span class="spawn-cred-value">{{ cred.masked_value }}</span>
          </label>
          <div class="spawn-repo-row spawn-cred-row spawn-cred-claude">
            <span
              class="spawn-cred-status"
              :class="{ 'spawn-cred-status--ok': credentials!.claude_credentials.saved }"
            >
              {{ credentials!.claude_credentials.saved ? '●' : '○' }}
            </span>
            <span class="spawn-repo-name">Claude OAuth credentials</span>
            <span class="spawn-cred-value">
              {{ credentials!.claude_credentials.saved ? 'configured' : 'not configured' }}
            </span>
          </div>
        </div>
        <p class="spawn-env-hint">Uncheck a credential to exclude it from this launch only.</p>
      </template>
      <label class="spawn-label">Env Vars <span class="spawn-hint">(optional)</span></label>
      <p class="spawn-env-hint">
        Key-value pairs are saved to the server and restored each session.
      </p>
      <div class="spawn-env-list">
        <div v-for="entry in visibleEnvVars" :key="entry.idx" class="spawn-env-row">
          <input
            v-model="entry.pair.key"
            class="spawn-input spawn-env-input spawn-env-key"
            placeholder="KEY"
            type="text"
          />
          <span class="spawn-env-sep">=</span>
          <input
            v-model="entry.pair.value"
            class="spawn-input spawn-env-input spawn-env-val"
            placeholder="value"
            type="text"
          />
          <button
            class="spawn-env-remove"
            @click="removeEnvVar(entry.idx)"
            type="button"
            title="Remove"
          >
            ×
          </button>
        </div>
        <button class="pixel-btn spawn-env-add" @click="addEnvVar" type="button">+ Add</button>
      </div>
      <div class="spawn-actions">
        <button
          class="pixel-btn spawn-launch-btn"
          :disabled="spawning || selectedRepos.length === 0"
          @click="launch"
        >
          {{ spawning ? 'Launching…' : 'Launch' }}
        </button>
      </div>
      <div v-if="error" class="spawn-error">{{ error }}</div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useGuildStore } from '../../stores/guild'
import { useGitHubStore } from '../../stores/github'
import { api } from '../../utils/api'
import { groupAndSortRepos } from '../../utils/repoGroups'

const emit = defineEmits<{ (e: 'launched'): void }>()

const guildStore = useGuildStore()
const ghStore = useGitHubStore()

const AVAILABLE_TOOLS = ['claude', 'codex', 'pi'] as const

interface EnvPair {
  key: string
  value: string
}

interface SpawnSettings {
  repos?: string[]
  tools?: string[]
  envVars?: EnvPair[]
}

interface GuildSpawnDefaults {
  repos: string[]
  tools: string[]
  agent_count: number | null
}

interface GuildEnvVarStatus {
  key: string
  masked_value: string
}

interface SpawnCredentials {
  guild_env_vars: GuildEnvVarStatus[]
  claude_credentials: { saved: boolean; updated_at: string | null }
}

const ENV_KEY_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/

const guildDefaults = ref<GuildSpawnDefaults | null>(null)
const usingGuildDefaults = ref(false)
const selectedRepos = ref<string[]>([])
const name = ref('')
const selectedTools = ref<string[]>([])
const agentCount = ref<number | null>(null)
const envVars = ref<EnvPair[]>([])
const spawning = ref(false)
const error = ref('')
const loadingRepos = ref(false)
const repoFetchFailed = ref(false)
const launched = ref(false)
const launchedWorkerId = ref('')
const credentials = ref<SpawnCredentials | null>(null)
const credentialsLoading = ref(false)
const credentialsError = ref('')
// Per-key opt-out for this launch only; a key defaults to included until unchecked.
const includedKeys = ref<Record<string, boolean>>({})

const hasCredentials = computed(
  () =>
    !!credentials.value &&
    (credentials.value.guild_env_vars.length > 0 || credentials.value.claude_credentials.saved),
)

const excludeEnvKeys = computed(() =>
  (credentials.value?.guild_env_vars ?? [])
    .map((c) => c.key)
    .filter((key) => includedKeys.value[key] === false),
)

const groupedRepos = computed(() => groupAndSortRepos(ghStore.repos))

// A key that already exists as a guild credential is shown/managed in the Guild
// Credentials section (with its value, checked by default) — don't also surface a
// stale blank personal row for it here, and don't let it override the credential.
const guildCredKeys = computed(
  () => new Set((credentials.value?.guild_env_vars ?? []).map((c) => c.key)),
)
const visibleEnvVars = computed(() =>
  envVars.value
    .map((pair, idx) => ({ pair, idx }))
    .filter((e) => !guildCredKeys.value.has(e.pair.key.trim())),
)

async function loadSavedSettings(guildId: string): Promise<SpawnSettings | null> {
  try {
    return await api<SpawnSettings>(`/guilds/${guildId}/spawn-settings`)
  } catch {
    return null
  }
}

async function loadGuildDefaults(guildId: string): Promise<GuildSpawnDefaults | null> {
  try {
    return await api<GuildSpawnDefaults>(`/guilds/${guildId}/spawn-defaults`)
  } catch {
    return null
  }
}

async function loadCredentials(guildId: string) {
  credentialsLoading.value = true
  credentialsError.value = ''
  try {
    const result = await api<SpawnCredentials>(`/guilds/${guildId}/spawn-credentials`)
    credentials.value = result
    for (const cred of result.guild_env_vars) {
      if (!(cred.key in includedKeys.value)) includedKeys.value[cred.key] = true
    }
  } catch (e: unknown) {
    credentialsError.value = e instanceof Error ? e.message : 'Failed to load credentials'
  } finally {
    credentialsLoading.value = false
  }
}

function toggleCredential(key: string) {
  includedKeys.value[key] = includedKeys.value[key] === false ? true : false
}

/** Mirrors the backend's env-key regex (`SpawnWorkerRequest`) so bad keys are
 *  caught before the request round-trip, and flags client-only duplicate keys
 *  the backend wouldn't otherwise reject (a later dict entry silently wins). */
function validateEnvVars(): string | null {
  const seen = new Set<string>()
  for (const pair of envVars.value) {
    const key = pair.key.trim()
    if (key === '') continue
    if (!ENV_KEY_PATTERN.test(key)) {
      return `Invalid env var key "${key}" — use letters, numbers, and underscores, starting with a letter or underscore.`
    }
    if (seen.has(key)) {
      return `Duplicate env var key "${key}".`
    }
    seen.add(key)
  }
  return null
}

/** Apply the guild defaults as the current form values, validating repos against
 *  what GitHub actually returned. Used both for the initial pre-fill (when the
 *  user has no saved overrides) and the explicit "reset to guild defaults" button. */
function applyGuildDefaults(defaults: GuildSpawnDefaults) {
  if (repoFetchFailed.value) {
    selectedRepos.value = []
  } else {
    const availableRepoNames = new Set(ghStore.repos.map((r) => r.full_name))
    selectedRepos.value = (defaults.repos ?? []).filter((r) => availableRepoNames.has(r))
  }
  selectedTools.value = [...(defaults.tools ?? [])]
  agentCount.value = defaults.agent_count ?? null
  usingGuildDefaults.value = true
}

function resetToGuildDefaults() {
  if (guildDefaults.value) applyGuildDefaults(guildDefaults.value)
}

const hasGuildDefaults = computed(
  () =>
    !!guildDefaults.value &&
    ((guildDefaults.value.repos?.length ?? 0) > 0 ||
      (guildDefaults.value.tools?.length ?? 0) > 0 ||
      guildDefaults.value.agent_count != null),
)

async function saveSettings(guildId: string) {
  try {
    await api(`/guilds/${guildId}/spawn-settings`, {
      method: 'PUT',
      json: {
        repos: selectedRepos.value,
        tools: selectedTools.value,
        // Persist only meaningful, non-duplicating pairs so a stale blank row
        // can't reappear and shadow a guild credential next session.
        envVars: envVars.value.filter(
          (e) => e.key.trim() !== '' && e.value.trim() !== '' && !guildCredKeys.value.has(e.key.trim()),
        ),
      },
    })
  } catch {
    // Settings persistence failure is non-fatal — the spawn already succeeded.
  }
}

onMounted(async () => {
  const guild = guildStore.currentGuild

  if (ghStore.repos.length === 0 && ghStore.token) {
    loadingRepos.value = true
    try {
      await ghStore.fetchRepos()
    } catch {
      repoFetchFailed.value = true
    } finally {
      loadingRepos.value = false
    }
  }

  const [saved, defaults] = guild
    ? await Promise.all([loadSavedSettings(guild.id), loadGuildDefaults(guild.id)])
    : [null, null]
  guildDefaults.value = defaults

  if (guild) {
    // Fire-and-forget: credential status isn't needed to render the rest of the form.
    loadCredentials(guild.id)
  }

  if (saved && (saved.repos?.length || saved.tools?.length || saved.envVars?.length)) {
    // The user has their own saved overrides — those win over the guild baseline.
    if (saved.repos?.length) {
      if (repoFetchFailed.value) {
        // Fetch failed — cannot validate saved repos against available ones.
        selectedRepos.value = []
      } else {
        const availableRepoNames = new Set(ghStore.repos.map((r) => r.full_name))
        // If fetch succeeded but returned no repos, the filter yields [] — correct.
        selectedRepos.value = saved.repos.filter((r) => availableRepoNames.has(r))
      }
    }
    if (saved.tools?.length) selectedTools.value = saved.tools
    if (saved.envVars?.length) envVars.value = saved.envVars
  } else if (defaults && hasGuildDefaults.value) {
    // No personal overrides yet — start from the guild's spawn defaults.
    applyGuildDefaults(defaults)
  } else {
    selectedRepos.value = repoFetchFailed.value ? [] : [...ghStore.selectedRepos]
  }
})

function toggleRepo(fullName: string) {
  usingGuildDefaults.value = false
  const idx = selectedRepos.value.indexOf(fullName)
  if (idx >= 0) {
    selectedRepos.value.splice(idx, 1)
  } else {
    selectedRepos.value.push(fullName)
  }
}

function orgAllSelected(owner: string): boolean {
  const repos = groupedRepos.value.find((g) => g.owner === owner)?.repos ?? []
  return repos.length > 0 && repos.every((r) => selectedRepos.value.includes(r.full_name))
}

function orgSomeSelected(owner: string): boolean {
  const repos = groupedRepos.value.find((g) => g.owner === owner)?.repos ?? []
  return repos.some((r) => selectedRepos.value.includes(r.full_name))
}

function toggleOrg(owner: string) {
  usingGuildDefaults.value = false
  const repos = groupedRepos.value.find((g) => g.owner === owner)?.repos ?? []
  if (orgAllSelected(owner)) {
    const names = new Set(repos.map((r) => r.full_name))
    selectedRepos.value = selectedRepos.value.filter((n) => !names.has(n))
  } else {
    for (const repo of repos) {
      if (!selectedRepos.value.includes(repo.full_name)) {
        selectedRepos.value.push(repo.full_name)
      }
    }
  }
}

function setOrgCheckboxRef(el: HTMLInputElement | null, owner: string) {
  if (el) {
    el.indeterminate = orgSomeSelected(owner) && !orgAllSelected(owner)
  }
}

function toggleTool(tool: string) {
  usingGuildDefaults.value = false
  const idx = selectedTools.value.indexOf(tool)
  if (idx >= 0) {
    selectedTools.value.splice(idx, 1)
  } else {
    selectedTools.value.push(tool)
  }
}

function addEnvVar() {
  envVars.value.push({ key: '', value: '' })
}

function removeEnvVar(idx: number) {
  envVars.value.splice(idx, 1)
}

async function launch() {
  const guild = guildStore.currentGuild
  if (!guild || selectedRepos.value.length === 0) return
  const validationError = validateEnvVars()
  if (validationError) {
    error.value = validationError
    return
  }
  spawning.value = true
  error.value = ''
  try {
    const envVarsPayload = Object.fromEntries(
      envVars.value
        // Drop blank keys, blank values (a blank pair sets nothing), and any key
        // that duplicates a guild credential (that value is the source of truth).
        .filter((e) => e.key.trim() !== '' && e.value.trim() !== '')
        .filter((e) => !guildCredKeys.value.has(e.key.trim()))
        .map((e) => [e.key.trim(), e.value.trim()]),
    )
    const result = await api<{ worker_id?: string }>(`/guilds/${guild.id}/spawn-worker`, {
      method: 'POST',
      json: {
        repos: selectedRepos.value,
        name: name.value.trim() || undefined,
        tools: selectedTools.value.length ? selectedTools.value : undefined,
        agent_count: agentCount.value ?? undefined,
        env_vars: Object.keys(envVarsPayload).length ? envVarsPayload : undefined,
        exclude_env_keys: excludeEnvKeys.value.length ? excludeEnvKeys.value : undefined,
      },
    })
    await saveSettings(guild.id)
    launchedWorkerId.value = result?.worker_id ?? ''
    launched.value = true
    setTimeout(() => emit('launched'), 2500)
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    spawning.value = false
  }
}
</script>

<style scoped>
.spawn-form {
  padding: 8px 10px;
  border-bottom: 1px solid var(--color-brass-dark);
  background: var(--color-bg-secondary);
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.spawn-success {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 12px 4px;
}

.spawn-success-label {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-green, #4caf50);
  letter-spacing: 1.5px;
  text-transform: uppercase;
}

.spawn-success-id {
  font-family: var(--font-mono, monospace);
  font-size: 11px;
  color: var(--color-teal);
  letter-spacing: 0.5px;
}

.spawn-label {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-teal);
  letter-spacing: 1px;
  text-transform: uppercase;
}

.spawn-hint {
  color: var(--color-text-dim);
  font-family: var(--font-mono, monospace);
  text-transform: none;
  letter-spacing: 0;
  font-size: 8px;
}

.spawn-input {
  background: var(--color-bg);
  border: 1px solid var(--color-brass-dark);
  color: var(--color-text);
  font-family: var(--font-mono, monospace);
  font-size: 10px;
  padding: 4px 6px;
  outline: none;
  border-radius: 2px;
  width: 100%;
  box-sizing: border-box;
}

.spawn-input:focus {
  border-color: var(--color-teal);
}

.spawn-input--narrow {
  width: 80px;
}

.spawn-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 2px;
}

.spawn-launch-btn {
  font-size: 7px;
  padding: 4px 10px;
}

.spawn-launch-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.spawn-error {
  font-size: 10px;
  color: var(--color-red);
  word-break: break-word;
}

.spawn-hint-text {
  font-size: 10px;
  color: var(--color-text-dim);
  padding: 4px 0;
  font-style: italic;
}

.spawn-repo-list {
  display: flex;
  flex-direction: column;
  gap: 1px;
  max-height: 160px;
  overflow-y: auto;
  border: 1px solid var(--color-brass-dark);
  border-radius: 2px;
  background: var(--color-bg);
}

.spawn-tool-list {
  display: flex;
  flex-direction: row;
  gap: 2px;
  border: 1px solid var(--color-brass-dark);
  border-radius: 2px;
  background: var(--color-bg);
}

.spawn-repo-row {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 5px 7px;
  cursor: pointer;
  border: 1px solid transparent;
  transition: background 0.1s;
}

.spawn-repo-row:hover {
  background: var(--color-bg-tertiary);
}

.spawn-repo-row.selected {
  background: rgba(232, 170, 0, 0.08);
  border-color: var(--color-brass-dark);
}

.spawn-org-row {
  background: var(--color-bg-secondary);
  border-bottom: 1px solid var(--color-brass-dark);
}

.spawn-org-row.selected {
  background: rgba(232, 170, 0, 0.12);
}

.spawn-org-name {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-teal);
  letter-spacing: 0.5px;
}

.spawn-repo-indent {
  padding-left: 22px;
}

.spawn-repo-check {
  accent-color: var(--color-brass);
  width: 12px;
  height: 12px;
  cursor: pointer;
  flex-shrink: 0;
}

.spawn-repo-name {
  font-size: 10px;
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.spawn-defaults-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  padding: 2px 0;
}

.spawn-defaults-note {
  font-size: 9px;
  color: var(--color-text-dim);
  font-style: italic;
}

.spawn-defaults-reset {
  background: none;
  border: none;
  color: var(--color-teal);
  cursor: pointer;
  font-family: var(--font-mono, monospace);
  font-size: 9px;
  padding: 0;
  text-decoration: underline;
  flex-shrink: 0;
}

.spawn-defaults-reset:hover {
  color: var(--color-brass-light);
}

.spawn-env-hint {
  font-size: 9px;
  color: var(--color-text-dim);
  font-style: italic;
  margin: 0 0 2px;
  line-height: 1.4;
}

.spawn-env-list {
  display: flex;
  flex-direction: column;
  gap: 3px;
  border: 1px solid var(--color-brass-dark);
  border-radius: 2px;
  background: var(--color-bg);
  padding: 4px;
}

.spawn-env-row {
  display: flex;
  align-items: center;
  gap: 3px;
}

.spawn-env-input {
  padding: 3px 4px;
  font-size: 9px;
  width: auto;
}

.spawn-env-key {
  width: 80px;
  flex-shrink: 0;
}

.spawn-env-val {
  flex: 1;
  min-width: 0;
}

.spawn-env-sep {
  font-size: 10px;
  color: var(--color-text-dim);
  flex-shrink: 0;
}

.spawn-env-remove {
  background: none;
  border: none;
  color: var(--color-red, #e53935);
  cursor: pointer;
  font-size: 14px;
  line-height: 1;
  padding: 0 2px;
  flex-shrink: 0;
}

.spawn-env-remove:hover {
  color: var(--color-text);
}

.spawn-env-add {
  font-size: 7px;
  padding: 3px 7px;
  align-self: flex-start;
  margin-top: 1px;
}

.spawn-cred-list {
  display: flex;
  flex-direction: column;
  gap: 1px;
  border: 1px solid var(--color-brass-dark);
  border-radius: 2px;
  background: var(--color-bg);
}

.spawn-cred-row {
  cursor: default;
}

.spawn-cred-key {
  flex-shrink: 0;
  font-family: var(--font-mono, monospace);
}

.spawn-cred-value {
  font-size: 9px;
  color: var(--color-text-dim);
  font-family: var(--font-mono, monospace);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.spawn-cred-claude {
  cursor: default;
  border-top: 1px solid var(--color-brass-dark);
}

.spawn-cred-status {
  color: var(--color-text-dim);
  font-size: 10px;
  flex-shrink: 0;
}

.spawn-cred-status--ok {
  color: var(--color-green, #4caf50);
}
</style>
