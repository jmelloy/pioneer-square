<template>
  <div class="settings-overlay" @mousedown.self="close">
    <div
      class="settings-panel"
      role="dialog"
      aria-modal="true"
      aria-label="Guild settings"
      ref="panelRef"
    >
      <header class="settings-panel-header">
        <div class="settings-panel-title">Guild Settings</div>
        <div class="settings-panel-guild" v-if="currentGuild">
          <span class="settings-panel-guild-name">{{ currentGuild.name || 'Unnamed Guild' }}</span>
          <span class="settings-panel-guild-id">#{{ currentGuild.id }}</span>
        </div>
        <button class="settings-close-btn" @click="close" title="Close settings">✕</button>
      </header>

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
          <!-- General -->
          <section v-if="activeTab === 'general'" class="settings-section">
            <div class="settings-field">
              <label class="settings-label">Name</label>
              <div class="settings-row">
                <input
                  v-model="renameValue"
                  class="settings-input"
                  @keydown.enter="commitRename"
                  @keydown.escape="close"
                />
                <button
                  class="pixel-btn settings-save-btn"
                  :disabled="
                    renameValue.trim() === (currentGuild?.name || '') || !renameValue.trim()
                  "
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
                  <option
                    v-for="repo in ghStore.repos"
                    :key="repo.full_name"
                    :value="repo.full_name"
                  >
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
              <code class="settings-meta-value">{{ currentGuild?.id }}</code>
            </div>
          </section>

          <!-- Foreman -->
          <section v-else-if="activeTab === 'foreman'" class="settings-section">
            <nav class="foreman-tool-tabs">
              <button
                v-for="ft in FOREMAN_TOOL_TABS"
                :key="ft.id"
                type="button"
                class="foreman-tool-tab"
                :class="{ active: foremanToolTab === ft.id }"
                @click="foremanToolTab = ft.id"
              >
                {{ ft.label }}
              </button>
            </nav>

            <!-- Claude: the foreman's own LLM (the AI orchestrator itself) -->
            <template v-if="foremanToolTab === 'claude'">
              <div class="foreman-field">
                <label class="foreman-field-label">Provider</label>
                <select v-model="foremanProvider" class="settings-input" @change="onProviderChange">
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
            </template>

            <!-- Pi: provider-agnostic tool, so it needs both a default model and provider -->
            <template v-else-if="foremanToolTab === 'pi'">
              <div class="foreman-field">
                <label class="foreman-field-label">Default Provider</label>
                <select v-model="piDefaultProvider" class="settings-input">
                  <option value="">default (anthropic)</option>
                  <option v-for="p in modelsStore.providers" :key="p.id" :value="p.id">
                    {{ p.name }}
                  </option>
                </select>
              </div>
              <div class="foreman-field">
                <label class="foreman-field-label">Default Model</label>
                <input
                  v-model="piDefaultModel"
                  class="settings-input"
                  list="pi-model-hints"
                  :placeholder="piDefaultProvider === 'bedrock' ? 'inference-profile ARN' : 'e.g. claude-sonnet-4-6'"
                  autocomplete="off"
                />
                <datalist id="pi-model-hints">
                  <option v-for="m in piProviderModels" :key="m.id" :value="m.id" :label="m.name" />
                </datalist>
              </div>
              <p class="foreman-hint">
                Used when the foreman assigns a task to the Pi tool without an explicit
                model/provider override. Select "Amazon Bedrock" to run Pi against Bedrock;
                its credentials are shared with the Claude tab's AWS Credentials fields.
              </p>
            </template>

            <!-- Codex -->
            <template v-else-if="foremanToolTab === 'codex'">
              <div class="foreman-field">
                <label class="foreman-field-label">Default Model</label>
                <input
                  v-model="codexDefaultModel"
                  class="settings-input"
                  placeholder="e.g. gpt-5-codex"
                  autocomplete="off"
                />
              </div>
              <p class="foreman-hint">
                Used when the foreman assigns a task to the Codex tool without an explicit
                model override.
              </p>
            </template>

            <!-- Per-tool environment: passed ONLY to the selected tool's runner,
                 never leaked to the other tools. -->
            <div class="foreman-field">
              <label class="foreman-field-label">{{ activeToolLabel }} Environment</label>
              <p class="foreman-hint">
                Passed only to the {{ activeToolLabel }} CLI — never to the other tools. Overrides
                the shared variables below for this tool. Pick a known variable or type your own.
              </p>
              <div class="env-var-list">
                <div
                  v-for="row in toolEnvRows[foremanToolTab]"
                  :key="row.id"
                  class="env-var-row"
                >
                  <input
                    v-model="row.key"
                    class="settings-input env-var-key"
                    :list="`tool-env-keys-${foremanToolTab}`"
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
                    @click="removeToolEnvVar(row)"
                    title="Remove variable"
                  >
                    ✕
                  </button>
                </div>
                <datalist :id="`tool-env-keys-${foremanToolTab}`">
                  <option v-for="k in TOOL_ENV_KEYS[foremanToolTab]" :key="k" :value="k" />
                </datalist>
                <button class="pixel-btn env-var-add-btn" @click="addToolEnvVar">
                  + Add Variable
                </button>
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
              <label class="foreman-field-label">Shared Environment Variables</label>
              <p class="foreman-hint">
                Inherited by every worker tool in this guild and the foreman's own LLM. Use the
                per-tool sections above to scope a variable to a single tool.
              </p>
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
              <span
                v-if="foremanStatus"
                class="save-status"
                :class="'save-status-' + foremanStatus"
              >
                {{ foremanStatus === 'saved' ? 'Saved' : 'Error' }}
              </span>
            </div>
          </section>

          <!-- Spawn Defaults -->
          <section v-else-if="activeTab === 'spawn'" class="settings-section">
            <GuildSpawnDefaults v-if="currentGuild" :guild-id="currentGuild.id" />
          </section>

          <!-- Members -->
          <section v-else-if="activeTab === 'members'" class="settings-section">
            <GuildMembers v-if="currentGuild" :guild-id="currentGuild.id" />
          </section>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, reactive, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useGuildStore } from '../stores/guild'
import { useGitHubStore } from '../stores/github'
import { useAuthStore } from '../stores/auth'
import { useModels } from '../composables/useModels'
import GuildMembers from './GuildMembers.vue'
import GuildSpawnDefaults from './GuildSpawnDefaults.vue'

const API_BASE = (import.meta.env.VITE_API_BASE as string) ?? ''

const emit = defineEmits<{ close: [] }>()

const guildStore = useGuildStore()
const ghStore = useGitHubStore()
const authStore = useAuthStore()

const currentGuild = computed(() => guildStore.currentGuild)

const TABS = [
  { id: 'general', label: 'General' },
  { id: 'foreman', label: 'Foreman' },
  { id: 'spawn', label: 'Spawn Defaults' },
  { id: 'members', label: 'Members' },
] as const
const activeTab = ref<(typeof TABS)[number]['id']>('general')

const FOREMAN_TOOL_TABS = [
  { id: 'claude', label: 'Claude' },
  { id: 'pi', label: 'Pi' },
  { id: 'codex', label: 'Codex' },
] as const
const foremanToolTab = ref<(typeof FOREMAN_TOOL_TABS)[number]['id']>('claude')
const activeToolLabel = computed(
  () => FOREMAN_TOOL_TABS.find((t) => t.id === foremanToolTab.value)?.label ?? '',
)

// Known env-var keys per worker tool, surfaced as datalist suggestions. Users can
// still type any other key. Sourced from each CLI's own docs (`pi --help`, Claude
// Code / Codex provider env vars).
const TOOL_ENV_KEYS: Record<string, string[]> = {
  claude: [
    'ANTHROPIC_API_KEY',
    'CLAUDE_CODE_OAUTH_TOKEN',
    'ANTHROPIC_AUTH_TOKEN',
    'ANTHROPIC_BASE_URL',
    'ANTHROPIC_MODEL',
    'CLAUDE_CODE_USE_BEDROCK',
    'AWS_REGION',
    'AWS_PROFILE',
    'AWS_ACCESS_KEY_ID',
    'AWS_SECRET_ACCESS_KEY',
    'AWS_SESSION_TOKEN',
    'AWS_BEARER_TOKEN_BEDROCK',
  ],
  codex: ['OPENAI_API_KEY', 'OPENAI_BASE_URL'],
  pi: [
    'ANTHROPIC_API_KEY',
    'ANTHROPIC_OAUTH_TOKEN',
    'OPENAI_API_KEY',
    'AZURE_OPENAI_API_KEY',
    'AZURE_OPENAI_BASE_URL',
    'AZURE_OPENAI_RESOURCE_NAME',
    'AZURE_OPENAI_API_VERSION',
    'AZURE_OPENAI_DEPLOYMENT_NAME_MAP',
    'DEEPSEEK_API_KEY',
    'GEMINI_API_KEY',
    'GROQ_API_KEY',
    'CEREBRAS_API_KEY',
    'XAI_API_KEY',
    'FIREWORKS_API_KEY',
    'TOGETHER_API_KEY',
    'OPENROUTER_API_KEY',
    'AI_GATEWAY_API_KEY',
    'ZAI_API_KEY',
    'MISTRAL_API_KEY',
    'MINIMAX_API_KEY',
    'MOONSHOT_API_KEY',
    'OPENCODE_API_KEY',
    'KIMI_API_KEY',
    'CLOUDFLARE_API_KEY',
    'CLOUDFLARE_ACCOUNT_ID',
    'CLOUDFLARE_GATEWAY_ID',
    'XIAOMI_API_KEY',
    'XIAOMI_TOKEN_PLAN_CN_API_KEY',
    'XIAOMI_TOKEN_PLAN_AMS_API_KEY',
    'XIAOMI_TOKEN_PLAN_SGP_API_KEY',
    'AWS_PROFILE',
    'AWS_ACCESS_KEY_ID',
    'AWS_SECRET_ACCESS_KEY',
    'AWS_BEARER_TOKEN_BEDROCK',
    'AWS_REGION',
  ],
}

const panelRef = ref<HTMLElement | null>(null)

const modelsStore = reactive(useModels())
const foremanProviderModels = computed(() =>
  foremanProvider.value ? modelsStore.modelsForProvider(foremanProvider.value) : [],
)
const piProviderModels = computed(() =>
  piDefaultProvider.value ? modelsStore.modelsForProvider(piDefaultProvider.value) : [],
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

const renameValue = ref('')
const primaryRepoValue = ref('')
const renameStatus = ref<'' | 'saved' | 'error'>('')
const repoStatus = ref<'' | 'saved' | 'error'>('')
let renameStatusTimer: ReturnType<typeof setTimeout> | null = null
let repoStatusTimer: ReturnType<typeof setTimeout> | null = null

const foremanModel = ref('')
const foremanProvider = ref('')
// Remember the model entered for each provider. Models are provider-specific
// (a Bedrock inference-profile ARN is invalid for the direct Anthropic API and
// vice versa), so switching providers swaps the model out — but toggling off a
// provider and back restores its model instead of wiping it.
const modelByProvider = ref<Record<string, string>>({})
let prevProvider = ''

function onProviderChange() {
  // v-model has already updated foremanProvider to the new value; foremanModel
  // still holds the previous provider's model, so stash it under prevProvider.
  modelByProvider.value[prevProvider] = foremanModel.value
  foremanModel.value = modelByProvider.value[foremanProvider.value] ?? ''
  prevProvider = foremanProvider.value
}
const piDefaultModel = ref('')
const piDefaultProvider = ref('')
const codexDefaultModel = ref('')
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

// Per-tool env var rows, keyed by tool id. Each tool's rows are saved under
// foreman_config.tool_env_vars[tool] and reach only that tool's runner.
const toolEnvRows = reactive<Record<string, EnvVarRow[]>>({ claude: [], pi: [], codex: [] })

function addToolEnvVar() {
  toolEnvRows[foremanToolTab.value].push({ id: ++envRowSeq, key: '', value: '' })
}

function removeToolEnvVar(row: EnvVarRow) {
  const rows = toolEnvRows[foremanToolTab.value]
  const i = rows.indexOf(row)
  if (i >= 0) rows.splice(i, 1)
}

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
      // Seed the per-provider model memory with the persisted pairing so a later
      // provider toggle can restore this model.
      prevProvider = foremanProvider.value
      modelByProvider.value = { [foremanProvider.value]: foremanModel.value }
      piDefaultModel.value = cfg.pi_default_model ?? ''
      piDefaultProvider.value = cfg.pi_default_provider ?? ''
      codexDefaultModel.value = cfg.codex_default_model ?? ''
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
      const toolEnv = cfg.tool_env_vars ?? {}
      for (const tool of ['claude', 'pi', 'codex']) {
        toolEnvRows[tool] = (toolEnv[tool] ?? []).map((e: { key: string; value?: string }) => ({
          id: ++envRowSeq,
          key: e.key,
          value: e.value ?? '',
        }))
      }
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
    if (piDefaultModel.value) body.pi_default_model = piDefaultModel.value
    else body.pi_default_model = null
    if (piDefaultProvider.value) body.pi_default_provider = piDefaultProvider.value
    else body.pi_default_provider = null
    if (codexDefaultModel.value) body.codex_default_model = codexDefaultModel.value
    else body.codex_default_model = null
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
    // A well-known key can be entered twice (dedicated field + a free-form row
    // that then hides itself); collapse duplicates, keeping the non-empty value
    // so a stray blank row can't shadow the real credential at spawn time.
    const envByKey = new Map<string, string>()
    for (const r of envVarRows.value) {
      const key = r.key.trim()
      if (!key) continue
      const existing = envByKey.get(key)
      if (existing && r.value === '' && existing !== '') continue
      envByKey.set(key, r.value)
    }
    body.env_vars = [...envByKey].map(([key, value]) => ({ key, value }))
    // Per-tool env vars: send all three tools so an emptied tab clears its set.
    const toolEnvVars: Record<string, { key: string; value: string }[]> = {}
    for (const tool of ['claude', 'pi', 'codex']) {
      const byKey = new Map<string, string>()
      for (const r of toolEnvRows[tool]) {
        const key = r.key.trim()
        if (!key) continue
        const existing = byKey.get(key)
        if (existing && r.value === '' && existing !== '') continue
        byKey.set(key, r.value)
      }
      toolEnvVars[tool] = [...byKey].map(([key, value]) => ({ key, value }))
    }
    body.tool_env_vars = toolEnvVars
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
      const savedToolEnv = saved.tool_env_vars ?? {}
      for (const tool of ['claude', 'pi', 'codex']) {
        toolEnvRows[tool] = (savedToolEnv[tool] ?? []).map(
          (e: { key: string; value?: string }) => ({
            id: ++envRowSeq,
            key: e.key,
            value: e.value ?? '',
          }),
        )
      }
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

function close() {
  emit('close')
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') close()
}

onMounted(async () => {
  document.addEventListener('keydown', onKeydown)
  renameValue.value = currentGuild.value?.name ?? ''
  primaryRepoValue.value = currentGuild.value?.primary_repo ?? ''
  if (ghStore.repos.length === 0 && ghStore.token) {
    await ghStore.fetchRepos()
  }
  modelsStore.loadModels()
  loadForemanConfig()
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown)
  if (renameStatusTimer) clearTimeout(renameStatusTimer)
  if (repoStatusTimer) clearTimeout(repoStatusTimer)
  if (foremanStatusTimer) clearTimeout(foremanStatusTimer)
})
</script>

<style scoped>
.settings-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 300;
  padding: 20px;
}

.settings-panel {
  width: min(920px, 94vw);
  height: min(620px, 88vh);
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
  width: 170px;
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
  overflow-y: auto;
  padding: 16px 18px;
}

.settings-section {
  display: flex;
  flex-direction: column;
  gap: 16px;
  max-width: 560px;
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

.foreman-tool-tabs {
  display: flex;
  gap: 4px;
  margin-bottom: 2px;
}

.foreman-tool-tab {
  background: none;
  border: 1px solid var(--color-brass-dark);
  color: var(--color-brass-dark);
  cursor: pointer;
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 1px;
  text-transform: uppercase;
  padding: 6px 12px;
  border-radius: 2px;
  transition:
    color 0.12s,
    background 0.12s,
    border-color 0.12s;
}

.foreman-tool-tab:hover {
  color: var(--color-brass);
  background: rgba(232, 170, 0, 0.06);
}

.foreman-tool-tab.active {
  color: var(--color-brass-light);
  background: rgba(232, 170, 0, 0.1);
  border-color: var(--color-brass);
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

.foreman-hint {
  font-family: var(--font-mono, monospace);
  font-size: 9px;
  color: var(--color-text-dim);
  font-style: italic;
  margin: 0 0 3px;
  line-height: 1.4;
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
  flex: 0 0 160px;
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

@media (max-width: 720px) {
  .settings-overlay {
    padding: 0;
  }

  .settings-panel {
    width: 100vw;
    height: 100dvh;
    max-height: 100dvh;
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
}

@media (prefers-reduced-motion: reduce) {
  .settings-close-btn,
  .settings-tab,
  .foreman-tool-tab,
  .env-var-delete-btn {
    transition: none;
  }
}
</style>
