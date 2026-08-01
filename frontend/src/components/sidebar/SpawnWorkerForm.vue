<template>
  <div class="settings-overlay" @mousedown.self="close">
    <div
      class="settings-panel spawn-panel"
      role="dialog"
      aria-modal="true"
      aria-label="Spawn worker"
      ref="panelRef"
    >
      <header class="settings-panel-header">
        <div class="settings-panel-title">Spawn Worker</div>
        <div class="settings-panel-guild" v-if="currentGuild">
          <span class="settings-panel-guild-name">{{ currentGuild.name || 'Unnamed Guild' }}</span>
          <span class="settings-panel-guild-id">#{{ currentGuild.id }}</span>
        </div>
        <button class="settings-close-btn" @click="close" title="Close">✕</button>
      </header>

      <div v-if="launched" class="spawn-success">
        <span class="spawn-success-label">Launched</span>
        <span class="spawn-success-id">{{ launchedWorkerId }}</span>
      </div>

      <template v-else>
        <div class="settings-panel-body">
          <nav class="settings-tabs">
            <button
              v-for="t in TABS"
              :key="t.id"
              class="settings-tab"
              :class="{ active: activeTab === t.id }"
              @click="activeTab = t.id"
            >
              {{ t.label }}
            </button>
          </nav>

          <div class="settings-content">
            <!-- General: identity + repo selection -->
            <section v-if="activeTab === 'general'" class="settings-section">
              <!-- Which layer the form is showing, and one click back to either
                   the guild baseline or this user's last launch. -->
              <div class="spawn-source">
                <span class="spawn-source-note">{{ sourceLabel }}</span>
                <span class="spawn-source-actions">
                  <button
                    v-if="hasGuildDefaults && formSource !== 'guild'"
                    class="spawn-defaults-reset"
                    type="button"
                    @click="resetToGuildDefaults"
                  >
                    Use guild defaults
                  </button>
                  <button
                    v-if="hasSavedSettings && formSource !== 'saved'"
                    class="spawn-defaults-reset"
                    type="button"
                    @click="restoreSavedSettings"
                  >
                    Use last launch
                  </button>
                </span>
              </div>

              <div class="settings-field">
                <label class="spawn-label">Name <span class="spawn-hint">(optional)</span></label>
                <input
                  v-model="name"
                  class="spawn-input"
                  type="text"
                  placeholder="auto-generated"
                />
              </div>

              <div class="settings-field">
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
                      <span v-if="guildDefaultRepos.has(repo.full_name)" class="spawn-badge">
                        guild
                      </span>
                    </label>
                  </template>
                </div>
                <div v-if="hasGuildDefaults" class="spawn-defaults-row">
                  <span class="spawn-defaults-note"> Guild default: {{ guildRepoSummary }} </span>
                </div>
              </div>
            </section>

            <!-- Tools: worker tools + concurrency -->
            <section v-else-if="activeTab === 'tools'" class="settings-section">
              <div class="settings-field">
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
                    <span v-if="guildDefaultTools.has(tool)" class="spawn-badge">guild</span>
                  </label>
                </div>
                <div v-if="hasGuildDefaults" class="spawn-defaults-row">
                  <span class="spawn-defaults-note">
                    Guild default:
                    {{ guildDefaults?.tools?.length ? guildDefaults.tools.join(', ') : 'none' }}
                  </span>
                </div>
              </div>

              <div class="settings-field">
                <label class="spawn-label">Agents <span class="spawn-hint">(optional)</span></label>
                <input
                  v-model.number="agentCount"
                  class="spawn-input spawn-input--narrow"
                  type="number"
                  min="1"
                  max="16"
                  :placeholder="String(guildDefaults?.agent_count ?? 4)"
                  @input="formSource = 'custom'"
                />
                <div class="spawn-defaults-row">
                  <span class="spawn-defaults-note">
                    {{
                      guildDefaults?.agent_count != null
                        ? `Guild default: ${guildDefaults.agent_count}`
                        : 'No guild default — the worker decides (4).'
                    }}
                  </span>
                </div>
              </div>
            </section>

            <!-- Environment: guild credentials + per-launch env vars -->
            <section v-else-if="activeTab === 'environment'" class="settings-section">
              <div class="settings-field">
                <label class="spawn-label"
                  >Guild Credentials <span class="spawn-hint">(optional)</span></label
                >
                <div v-if="credentialsLoading" class="spawn-hint-text">Loading credentials…</div>
                <div v-else-if="credentialsError" class="spawn-error">{{ credentialsError }}</div>
                <div v-else-if="!hasCredentials" class="spawn-hint-text">
                  No guild credentials configured.
                </div>
                <template v-else>
                  <div class="spawn-cred-list">
                    <label
                      v-for="cred in credentials!.guild_env_vars"
                      :key="cred.key"
                      class="spawn-repo-row spawn-cred-row"
                      :class="{
                        selected: includedKeys[cred.key] !== false,
                        'spawn-cred-overridden': overriddenGuildKeys.has(cred.key),
                      }"
                    >
                      <input
                        type="checkbox"
                        :checked="
                          includedKeys[cred.key] !== false || overriddenGuildKeys.has(cred.key)
                        "
                        :disabled="overriddenGuildKeys.has(cred.key)"
                        @change="toggleCredential(cred.key)"
                        class="spawn-repo-check"
                      />
                      <span class="spawn-repo-name spawn-cred-key">{{ cred.key }}</span>
                      <span v-if="overriddenGuildKeys.has(cred.key)" class="spawn-badge">
                        overridden below
                      </span>
                      <span v-else class="spawn-cred-value">{{ cred.masked_value }}</span>
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
                        {{
                          credentials!.claude_credentials.saved ? 'configured' : 'not configured'
                        }}
                      </span>
                    </div>
                  </div>
                  <p class="spawn-env-hint">
                    Uncheck a credential to exclude it from this launch only.
                  </p>
                </template>
              </div>

              <div class="settings-field">
                <label class="spawn-label"
                  >Your Env Vars <span class="spawn-hint">(optional)</span></label
                >
                <p class="spawn-env-hint">
                  Your own key-value pairs, saved on launch and restored next time. Reusing a guild
                  credential's key replaces its value for your workers.
                </p>
                <div class="spawn-env-list">
                  <div v-for="(pair, idx) in envVars" :key="idx" class="spawn-env-row">
                    <input
                      v-model="pair.key"
                      class="spawn-input spawn-env-input spawn-env-key"
                      placeholder="KEY"
                      type="text"
                    />
                    <span class="spawn-env-sep">=</span>
                    <input
                      v-model="pair.value"
                      class="spawn-input spawn-env-input spawn-env-val"
                      placeholder="value"
                      type="text"
                    />
                    <span
                      v-if="guildCredKeys.has(pair.key.trim())"
                      class="spawn-badge spawn-badge--override"
                      >overrides guild</span
                    >
                    <button
                      class="spawn-env-remove"
                      @click="removeEnvVar(idx)"
                      type="button"
                      title="Remove"
                    >
                      ×
                    </button>
                  </div>
                  <button class="pixel-btn spawn-env-add" @click="addEnvVar" type="button">
                    + Add
                  </button>
                </div>
              </div>

              <!-- Per-tool overrides: passed only to the selected tool's runner,
                   never the others; overrides a shared Env Var for that tool.
                   Mirrors Guild Settings → Worker Tools. -->
              <div class="settings-field">
                <label class="spawn-label"
                  >Per-Tool Overrides <span class="spawn-hint">(optional)</span></label
                >
                <nav class="spawn-tool-tabs">
                  <button
                    v-for="tool in AVAILABLE_TOOLS"
                    :key="tool"
                    type="button"
                    class="spawn-tool-tab"
                    :class="{ active: envToolTab === tool }"
                    @click="envToolTab = tool"
                  >
                    {{ tool }}
                  </button>
                </nav>
                <p class="spawn-env-hint">
                  Passed only to the {{ envToolTab }} CLI — never the other tools. Overrides an Env
                  Var above for this tool.
                </p>
                <div v-if="guildToolEnv.length" class="spawn-cred-list spawn-tool-guild-list">
                  <div
                    v-for="cred in guildToolEnv"
                    :key="cred.key"
                    class="spawn-repo-row spawn-cred-row"
                  >
                    <span class="spawn-badge">guild</span>
                    <span class="spawn-repo-name spawn-cred-key">{{ cred.key }}</span>
                    <span class="spawn-cred-value">{{ cred.masked_value }}</span>
                  </div>
                </div>
                <div class="spawn-env-list">
                  <div
                    v-for="(pair, idx) in toolEnvVars[envToolTab]"
                    :key="idx"
                    class="spawn-env-row"
                  >
                    <input
                      v-model="pair.key"
                      class="spawn-input spawn-env-input spawn-env-key"
                      placeholder="KEY"
                      type="text"
                    />
                    <span class="spawn-env-sep">=</span>
                    <input
                      v-model="pair.value"
                      class="spawn-input spawn-env-input spawn-env-val"
                      placeholder="value"
                      type="text"
                    />
                    <button
                      class="spawn-env-remove"
                      @click="removeToolEnvVar(idx)"
                      type="button"
                      title="Remove"
                    >
                      ×
                    </button>
                  </div>
                  <button class="pixel-btn spawn-env-add" @click="addToolEnvVar" type="button">
                    + Add
                  </button>
                </div>
              </div>
            </section>
          </div>
        </div>

        <footer class="spawn-panel-footer">
          <span v-if="error" class="spawn-error">{{ error }}</span>
          <button
            class="pixel-btn spawn-launch-btn"
            :disabled="spawning || selectedRepos.length === 0"
            @click="launch"
          >
            {{ spawning ? 'Launching…' : 'Launch' }}
          </button>
        </footer>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, onBeforeUnmount } from 'vue'
import { useGuildStore } from '../../stores/guild'
import { useGitHubStore } from '../../stores/github'
import { api } from '../../utils/api'
import { groupAndSortRepos } from '../../utils/repoGroups'

const emit = defineEmits<{ (e: 'launched'): void; (e: 'close'): void }>()

const guildStore = useGuildStore()
const ghStore = useGitHubStore()

const currentGuild = computed(() => guildStore.currentGuild)

const TABS = [
  { id: 'general', label: 'General' },
  { id: 'tools', label: 'Tools' },
  { id: 'environment', label: 'Environment' },
] as const
const activeTab = ref<(typeof TABS)[number]['id']>('general')

const panelRef = ref<HTMLElement | null>(null)

const AVAILABLE_TOOLS = ['claude', 'codex', 'pi'] as const

interface EnvPair {
  key: string
  value: string
}

interface SpawnSettings {
  repos?: string[]
  tools?: string[]
  envVars?: EnvPair[]
  toolEnvVars?: Record<string, EnvPair[]>
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
  // The guild baseline only — the caller's own vars come back in clear text
  // from /spawn-settings so they stay editable here.
  guild_env_vars: GuildEnvVarStatus[]
  guild_tool_env_vars?: Record<string, GuildEnvVarStatus[]>
  claude_credentials: { saved: boolean; updated_at: string | null }
}

const ENV_KEY_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/

const guildDefaults = ref<GuildSpawnDefaults | null>(null)
// The caller's own settings from their last launch, kept so they can be
// restored after an experiment with the guild baseline.
const savedSettings = ref<SpawnSettings | null>(null)
// Which layer the form currently shows: the guild baseline, this user's last
// launch, or values they have since edited.
const formSource = ref<'guild' | 'saved' | 'custom'>('custom')
const selectedRepos = ref<string[]>([])
const name = ref('')
const selectedTools = ref<string[]>([])
const agentCount = ref<number | null>(null)
const envVars = ref<EnvPair[]>([])
// Per-tool override env, keyed by tool. Reaches only that tool's runner via the
// worker's /foreman/env-vars fetch (resolved against this user's spawn row).
const toolEnvVars = reactive<Record<string, EnvPair[]>>({ claude: [], codex: [], pi: [] })
const envToolTab = ref<(typeof AVAILABLE_TOOLS)[number]>('claude')
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

const groupedRepos = computed(() => groupAndSortRepos(ghStore.repos))

const guildCredKeys = computed(
  () => new Set((credentials.value?.guild_env_vars ?? []).map((c) => c.key)),
)
// Keys this user has given their own value for. The backend layers the user
// over the guild, so these replace the guild credential for this launch.
const userEnvKeys = computed(
  () => new Set(envVars.value.map((e) => e.key.trim()).filter((k) => k !== '')),
)
const overriddenGuildKeys = computed(
  () => new Set([...guildCredKeys.value].filter((k) => userEnvKeys.value.has(k))),
)

// An overridden key is replaced, not dropped — excluding it would contradict the
// value the user just typed (and the backend keeps the explicit value anyway).
const excludeEnvKeys = computed(() =>
  (credentials.value?.guild_env_vars ?? [])
    .map((c) => c.key)
    .filter((key) => includedKeys.value[key] === false && !overriddenGuildKeys.value.has(key)),
)

// The guild's scoped vars for the tool tab on screen, shown read-only above the
// user's own overrides for the same tool.
const guildToolEnv = computed<GuildEnvVarStatus[]>(
  () => credentials.value?.guild_tool_env_vars?.[envToolTab.value] ?? [],
)

const guildDefaultRepos = computed(() => new Set(guildDefaults.value?.repos ?? []))
const guildDefaultTools = computed(() => new Set(guildDefaults.value?.tools ?? []))

const hasSavedSettings = computed(() => {
  const s = savedSettings.value
  if (!s) return false
  return !!(
    s.repos?.length ||
    s.tools?.length ||
    s.envVars?.length ||
    Object.values(s.toolEnvVars ?? {}).some((p) => p.length)
  )
})

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
function validatePairs(pairs: EnvPair[], label: string): string | null {
  const seen = new Set<string>()
  for (const pair of pairs) {
    const key = pair.key.trim()
    if (key === '') continue
    if (!ENV_KEY_PATTERN.test(key)) {
      return `Invalid ${label} key "${key}" — use letters, numbers, and underscores, starting with a letter or underscore.`
    }
    if (seen.has(key)) {
      return `Duplicate ${label} key "${key}".`
    }
    seen.add(key)
  }
  return null
}

function validateEnvVars(): string | null {
  const flat = validatePairs(envVars.value, 'env var')
  if (flat) return flat
  for (const tool of AVAILABLE_TOOLS) {
    const err = validatePairs(toolEnvVars[tool], `${tool} override`)
    if (err) return err
  }
  return null
}

/** Keep only repos GitHub actually returned; a fetch failure means we can't
 *  validate any of them, so none are pre-selected. */
function availableOnly(repos: string[] | undefined): string[] {
  if (repoFetchFailed.value) return []
  const availableRepoNames = new Set(ghStore.repos.map((r) => r.full_name))
  return (repos ?? []).filter((r) => availableRepoNames.has(r))
}

/** Apply the guild defaults as the current form values. Used both for the
 *  initial pre-fill (when the user has no saved settings) and the explicit
 *  "reset to guild defaults" button. Leaves env vars alone: those are the
 *  user's own, and the guild's are shown separately in the Environment tab. */
function applyGuildDefaults(defaults: GuildSpawnDefaults) {
  selectedRepos.value = availableOnly(defaults.repos)
  selectedTools.value = [...(defaults.tools ?? [])]
  agentCount.value = defaults.agent_count ?? null
  formSource.value = 'guild'
}

/** Apply this user's last-used settings, falling back to the guild baseline for
 *  anything they never set. */
function applySavedSettings(saved: SpawnSettings) {
  if (saved.repos?.length) selectedRepos.value = availableOnly(saved.repos)
  else selectedRepos.value = availableOnly(guildDefaults.value?.repos)
  if (saved.tools?.length) selectedTools.value = [...saved.tools]
  else selectedTools.value = [...(guildDefaults.value?.tools ?? [])]
  agentCount.value = guildDefaults.value?.agent_count ?? null
  envVars.value = (saved.envVars ?? []).map((p) => ({ ...p }))
  for (const tool of AVAILABLE_TOOLS) {
    toolEnvVars[tool] = (saved.toolEnvVars?.[tool] ?? []).map((p) => ({ ...p }))
  }
  formSource.value = 'saved'
}

function resetToGuildDefaults() {
  if (guildDefaults.value) applyGuildDefaults(guildDefaults.value)
}

function restoreSavedSettings() {
  if (savedSettings.value) applySavedSettings(savedSettings.value)
}

const hasGuildDefaults = computed(
  () =>
    !!guildDefaults.value &&
    ((guildDefaults.value.repos?.length ?? 0) > 0 ||
      (guildDefaults.value.tools?.length ?? 0) > 0 ||
      guildDefaults.value.agent_count != null),
)

const guildRepoSummary = computed(() => {
  const repos = guildDefaults.value?.repos ?? []
  if (repos.length === 0) return 'none'
  if (repos.length <= 3) return repos.join(', ')
  return `${repos.slice(0, 3).join(', ')} +${repos.length - 3} more`
})

const sourceLabel = computed(() => {
  if (formSource.value === 'guild') return 'Showing the guild defaults.'
  if (formSource.value === 'saved') return 'Showing your settings from your last launch.'
  return hasGuildDefaults.value || hasSavedSettings.value
    ? 'Showing your edits for this launch.'
    : 'No guild defaults or saved settings yet — pick repos to launch.'
})

async function saveSettings(guildId: string) {
  try {
    await api(`/guilds/${guildId}/spawn-settings`, {
      method: 'PUT',
      json: {
        repos: selectedRepos.value,
        tools: selectedTools.value,
        // Persist only meaningful pairs so a stale blank row can't reappear.
        // A pair whose key matches a guild credential is kept on purpose: it is
        // this user's deliberate override and must survive to the next launch.
        envVars: envVars.value.filter((e) => e.key.trim() !== '' && e.value.trim() !== ''),
        toolEnvVars: Object.fromEntries(
          AVAILABLE_TOOLS.map((tool) => [
            tool,
            toolEnvVars[tool].filter((e) => e.key.trim() !== '' && e.value.trim() !== ''),
          ]),
        ),
      },
    })
  } catch {
    // Settings persistence failure is non-fatal — the spawn already succeeded.
  }
}

function close() {
  emit('close')
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') close()
}

onMounted(async () => {
  document.addEventListener('keydown', onKeydown)

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

  savedSettings.value = saved

  if (hasSavedSettings.value) {
    // The user has settings from a previous launch — those win over the guild
    // baseline, and "Reset to guild defaults" undoes that.
    applySavedSettings(saved!)
  } else if (defaults && hasGuildDefaults.value) {
    // Nothing saved yet — start from the guild's spawn defaults.
    applyGuildDefaults(defaults)
  } else {
    selectedRepos.value = repoFetchFailed.value ? [] : [...ghStore.selectedRepos]
    formSource.value = 'custom'
  }
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown)
})

function toggleRepo(fullName: string) {
  formSource.value = 'custom'
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
  formSource.value = 'custom'
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
  formSource.value = 'custom'
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

function addToolEnvVar() {
  toolEnvVars[envToolTab.value].push({ key: '', value: '' })
}

function removeToolEnvVar(idx: number) {
  toolEnvVars[envToolTab.value].splice(idx, 1)
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
        // Drop blank keys and blank values (a blank pair sets nothing). A key
        // that also exists as a guild credential is sent: the backend layers the
        // caller's value over the guild's, which is the point of an override.
        .filter((e) => e.key.trim() !== '' && e.value.trim() !== '')
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
.settings-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  /* FactoryFloor robots/stations use z-index up to ~1000 (Math.round(y * 1000)),
     so the overlay must sit above that to stay clickable. */
  z-index: 2000;
  padding: 20px;
}

.settings-panel {
  width: min(760px, 94vw);
  height: min(560px, 88vh);
  background: var(--color-bg-secondary);
  border: 2px solid var(--color-brass);
  box-shadow:
    0 6px 24px rgba(0, 0, 0, 0.5),
    0 0 16px rgba(232, 170, 0, 0.15);
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.settings-panel-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 14px;
  border-bottom: 2px solid var(--color-brass-dark);
  flex-shrink: 0;
}

.settings-panel-title {
  font-family: var(--font-pixel);
  font-size: 9px;
  color: var(--color-brass-light);
  letter-spacing: 2px;
  text-transform: uppercase;
  flex-shrink: 0;
}

.settings-panel-guild {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
  flex: 1;
}

.settings-panel-guild-name {
  font-family: var(--font-pixel);
  font-size: 8px;
  color: var(--color-brass);
  letter-spacing: 1px;
  text-transform: uppercase;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.settings-panel-guild-id {
  font-family: var(--font-mono, monospace);
  font-size: 10px;
  color: var(--color-text-dim);
  flex-shrink: 0;
}

.settings-close-btn {
  background: var(--color-bg);
  border: 1px solid var(--color-brass-dark);
  color: var(--color-brass);
  cursor: pointer;
  width: 28px;
  height: 28px;
  border-radius: 2px;
  font-size: 12px;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition:
    border-color 0.12s,
    background 0.12s,
    color 0.12s;
}

.settings-close-btn:hover {
  border-color: var(--color-brass);
  background: rgba(232, 170, 0, 0.1);
  color: var(--color-brass-light);
}

.settings-panel-body {
  display: flex;
  flex: 1;
  min-height: 0;
}

.settings-tabs {
  display: flex;
  flex-direction: column;
  gap: 2px;
  width: 150px;
  flex-shrink: 0;
  padding: 10px 8px;
  border-right: 1px solid var(--color-brass-dark);
  background: var(--color-bg);
  overflow-y: auto;
}

.settings-tab {
  background: none;
  border: 1px solid transparent;
  color: var(--color-brass-dark);
  cursor: pointer;
  font-family: var(--font-pixel);
  font-size: 7px;
  letter-spacing: 1px;
  text-transform: uppercase;
  text-align: left;
  padding: 9px 10px;
  border-radius: 2px;
  transition:
    color 0.12s,
    background 0.12s,
    border-color 0.12s;
}

.settings-tab:hover {
  color: var(--color-brass);
  background: rgba(232, 170, 0, 0.06);
}

.settings-tab.active {
  color: var(--color-brass-light);
  background: rgba(232, 170, 0, 0.1);
  border-color: var(--color-brass-dark);
}

.settings-content {
  flex: 1;
  min-width: 0;
  /* Flex items default to min-height: auto, which lets this grow to its
     content height instead of shrinking to the container — on mobile,
     where settings-panel-body stacks as a column, that pushes the footer
     (and the Launch button) off the bottom of the viewport instead of
     scrolling internally. */
  min-height: 0;
  overflow-y: auto;
  padding: 14px 16px;
}

.settings-section {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.settings-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.spawn-panel-footer {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  /* A long error must wrap onto its own line rather than squeeze the Launch
     button off the edge — on a phone there is no horizontal slack at all. */
  flex-wrap: wrap;
  gap: 10px;
  padding: 10px 16px;
  border-top: 1px solid var(--color-brass-dark);
  flex-shrink: 0;
}

.spawn-panel-footer .spawn-error {
  flex: 1 1 100%;
  min-width: 0;
}

.spawn-success {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  flex: 1;
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

.spawn-launch-btn {
  font-size: 7px;
  padding: 5px 12px;
  flex-shrink: 0;
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
  max-height: 220px;
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

.spawn-tool-tabs {
  display: flex;
  gap: 4px;
  margin-bottom: 4px;
}

.spawn-tool-tab {
  background: none;
  border: 1px solid var(--color-brass-dark);
  color: var(--color-brass-dark);
  cursor: pointer;
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 1px;
  text-transform: uppercase;
  padding: 5px 10px;
  border-radius: 2px;
  transition:
    color 0.12s,
    background 0.12s,
    border-color 0.12s;
}

.spawn-tool-tab:hover {
  color: var(--color-brass);
  background: rgba(232, 170, 0, 0.06);
}

.spawn-tool-tab.active {
  color: var(--color-brass-light);
  background: rgba(232, 170, 0, 0.1);
  border-color: var(--color-brass);
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
  flex-wrap: wrap;
  gap: 6px;
  padding: 2px 0;
}

.spawn-source {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 6px;
  padding: 6px 8px;
  border: 1px solid var(--color-brass-dark);
  border-radius: 2px;
  background: var(--color-bg);
}

.spawn-source-note {
  font-size: 9px;
  color: var(--color-text-dim);
  font-style: italic;
  min-width: 0;
}

.spawn-source-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}

.spawn-badge {
  font-family: var(--font-mono, monospace);
  font-size: 8px;
  color: var(--color-brass);
  border: 1px solid var(--color-brass-dark);
  border-radius: 2px;
  padding: 0 3px;
  flex-shrink: 0;
  white-space: nowrap;
}

.spawn-badge--override {
  color: var(--color-teal);
}

.spawn-cred-overridden .spawn-cred-key {
  text-decoration: line-through;
  color: var(--color-text-dim);
}

.spawn-tool-guild-list {
  margin-bottom: 4px;
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
  width: 100px;
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

@media (max-width: 720px) {
  .settings-overlay {
    padding: 0;
    /* Fill the overlay instead of centring a fixed-height panel inside it. The
       old panel sized itself with 100dvh while the overlay is sized by the
       fixed-position viewport; on iOS Safari those two are not the same box, so
       a centred panel overflowed top AND bottom and took the footer — with the
       Launch button — off screen. Sizing off the overlay can't disagree. */
    align-items: stretch;
    justify-content: stretch;
  }

  .settings-panel {
    width: 100%;
    height: 100%;
    max-height: 100%;
    border: none;
  }

  .settings-panel-body {
    flex-direction: column;
  }

  .settings-tabs {
    flex-direction: row;
    width: auto;
    gap: 4px;
    border-right: none;
    border-bottom: 1px solid var(--color-brass-dark);
    overflow-x: auto;
    overflow-y: hidden;
  }

  .settings-tab {
    white-space: nowrap;
    flex-shrink: 0;
  }

  /* Keep the Launch button clear of the home-indicator / gesture-bar
     area on notched phones in a full-screen dialog. */
  .spawn-panel-footer {
    padding-bottom: max(10px, env(safe-area-inset-bottom));
    padding-left: max(16px, env(safe-area-inset-left));
    padding-right: max(16px, env(safe-area-inset-right));
  }

  .settings-panel-header {
    padding-left: max(14px, env(safe-area-inset-left));
    padding-right: max(14px, env(safe-area-inset-right));
  }

  .settings-content {
    padding-left: max(16px, env(safe-area-inset-left));
    padding-right: max(16px, env(safe-area-inset-right));
    /* Stop a rubber-banding scroll here from dragging the page behind it. */
    overscroll-behavior: contain;
    -webkit-overflow-scrolling: touch;
  }

  /* The panel is the whole screen: let the repo list use the space it has
     rather than capping at a desktop-sized box with its own scrollbar. */
  .spawn-repo-list {
    max-height: 46vh;
  }

  /* A 12px checkbox is well under the ~44px minimum tap target; the row is the
     real target, so give it height and the box enough size to hit reliably. */
  .spawn-repo-row {
    padding: 8px 7px;
  }

  .spawn-repo-check {
    width: 16px;
    height: 16px;
  }

  /* KEY = VALUE doesn't fit one phone-width line — stack the two inputs, and
     rule off each pair so the stacked key and value still read as one row. */
  .spawn-env-row {
    flex-wrap: wrap;
    padding-bottom: 5px;
    border-bottom: 1px solid var(--color-brass-dark);
  }

  .spawn-env-row:last-of-type {
    border-bottom: none;
    padding-bottom: 0;
  }

  .spawn-env-key {
    width: auto;
    flex: 1 1 100%;
  }

  .spawn-env-sep {
    display: none;
  }

  .spawn-env-val {
    flex: 1 1 auto;
  }

  .spawn-launch-btn {
    font-size: 8px;
    padding: 10px 20px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .settings-close-btn,
  .settings-tab {
    transition: none;
  }
}
</style>
