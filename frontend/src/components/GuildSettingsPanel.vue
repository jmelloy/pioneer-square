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

            <div class="settings-field">
              <label class="settings-label">GitHub App Installation ID</label>
              <p class="settings-hint">
                From github.com/settings/installations/&lt;id&gt;. Attributes comments and commits
                to the app bot. Blank uses the server default.
              </p>
              <div class="settings-row">
                <input
                  v-model="githubAppInstallationId"
                  type="text"
                  inputmode="numeric"
                  placeholder="e.g. 149025614"
                  class="settings-input"
                  @keydown.enter="saveGithubAppInstallation"
                  @keydown.escape="close"
                />
                <button
                  class="pixel-btn settings-save-btn"
                  :disabled="appInstallSaving || githubAppInstallationId.trim() === savedInstallId"
                  @click="saveGithubAppInstallation"
                >
                  {{ appInstallSaving ? 'Saving…' : 'Save' }}
                </button>
                <span
                  v-if="appInstallStatus"
                  class="save-status"
                  :class="'save-status-' + appInstallStatus"
                >
                  {{ appInstallStatus === 'saved' ? 'Saved' : 'Error' }}
                </span>
              </div>
            </div>

            <div class="settings-field settings-meta">
              <span class="settings-meta-label">Session ID</span>
              <code class="settings-meta-value">{{ currentGuild?.id }}</code>
            </div>
          </section>

          <!-- Foreman — the orchestrator AI itself (its LLM + orchestration knobs).
               Per-worker-tool defaults/env live under Worker Settings. -->
          <section v-else-if="activeTab === 'foreman'" class="settings-section">
            <p class="foreman-hint">
              These configure the foreman's own orchestrator LLM (the AI itself). Fields left blank
              fall back to the server defaults shown below.
            </p>
            <div class="foreman-field">
              <label class="foreman-field-label">Provider</label>
              <select v-model="foremanProvider" class="settings-input" @change="onProviderChange">
                <option value="">{{ providerDefaultLabel }}</option>
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
                :placeholder="foremanModelPlaceholder"
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

            <div class="foreman-divider">Orchestration</div>

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
              <label class="foreman-field-label">Environment Variables</label>
              <p class="foreman-hint">
                Used only by the foreman's own LLM (credentials, base URLs, etc.) — these are
                <em>not</em> sent to workers. To give a variable to every worker, add it under
                Worker Settings → General; to scope it to one tool, use the Claude / Pi / Codex
                tabs.
              </p>
              <p v-if="envDefaultKeys.length" class="foreman-hint">
                From the server environment (masked, always available to the foreman):
                {{ envDefaultsSummary }}
              </p>
              <div class="env-var-list">
                <div class="env-var-row env-var-head">
                  <span class="env-var-key">Key</span>
                  <span class="env-var-value">Value</span>
                  <span class="env-var-spacer"></span>
                </div>
                <div v-for="row in foremanEnvRows" :key="row.id" class="env-var-row">
                  <input
                    v-model="row.key"
                    class="settings-input env-var-key"
                    list="foreman-env-keys"
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
                    @click="removeEnvRow(foremanEnvRows, row)"
                    title="Remove variable"
                  >
                    ✕
                  </button>
                </div>
                <datalist id="foreman-env-keys">
                  <option v-for="k in FOREMAN_ENV_KEYS" :key="k" :value="k" />
                </datalist>
                <button class="pixel-btn env-var-add-btn" @click="addEnvRow(foremanEnvRows)">
                  + Add Variable
                </button>
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

          <!-- Worker Settings — spawn defaults + per-worker-tool model/env config -->
          <section v-else-if="activeTab === 'spawn'" class="settings-section">
            <GuildSpawnDefaults v-if="currentGuild" :guild-id="currentGuild.id" />

            <div class="foreman-divider">Spawn Profiles</div>
            <GuildSpawnProfiles v-if="currentGuild" :guild-id="currentGuild.id" />

            <div class="foreman-divider">Worker Tools</div>
            <nav class="foreman-tool-tabs">
              <button
                v-for="ft in WORKER_SUBTABS"
                :key="ft.id"
                type="button"
                class="foreman-tool-tab"
                :class="{ active: workerSubTab === ft.id }"
                @click="workerSubTab = ft.id"
              >
                {{ ft.label }}
              </button>
            </nav>

            <!-- General: env vars every worker tool in this guild receives -->
            <template v-if="workerSubTab === 'general'">
              <p class="foreman-hint">
                Every worker tool in this guild inherits these variables. Use the Claude / Pi /
                Codex tabs to override a value for a single tool.
              </p>
              <div class="env-var-list">
                <div class="env-var-row env-var-head">
                  <span class="env-var-key">Key</span>
                  <span class="env-var-value">Value</span>
                  <span class="env-var-spacer"></span>
                </div>
                <div v-for="row in workerEnvRows" :key="row.id" class="env-var-row">
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
                    @click="removeEnvRow(workerEnvRows, row)"
                    title="Remove variable"
                  >
                    ✕
                  </button>
                </div>
                <button class="pixel-btn env-var-add-btn" @click="addEnvRow(workerEnvRows)">
                  + Add Variable
                </button>
              </div>
            </template>

            <!-- Pi: provider-agnostic tool, so it needs a provider + model override -->
            <template v-else-if="workerSubTab === 'pi'">
              <div class="foreman-field">
                <label class="foreman-field-label">Provider Override</label>
                <select v-model="piDefaultProvider" class="settings-input">
                  <option value="">default (anthropic)</option>
                  <option v-for="p in modelsStore.providers" :key="p.id" :value="p.id">
                    {{ p.name }}
                  </option>
                </select>
              </div>
              <div class="foreman-field">
                <label class="foreman-field-label">Model Override</label>
                <input
                  v-model="piDefaultModel"
                  class="settings-input"
                  list="pi-model-hints"
                  :placeholder="
                    piDefaultProvider === 'bedrock'
                      ? 'inference-profile ARN'
                      : 'e.g. claude-sonnet-4-6'
                  "
                  autocomplete="off"
                />
                <datalist id="pi-model-hints">
                  <option v-for="m in piProviderModels" :key="m.id" :value="m.id" :label="m.name" />
                </datalist>
              </div>
              <p class="foreman-hint">
                Overrides the model Pi runs when the foreman assigns it a task without an explicit
                model/provider. Setting a provider forces Pi onto that provider's models (Pi ignores
                a bare provider unless the model is pinned too). Select "Amazon Bedrock" to run Pi
                against Bedrock; set its AWS credentials in the Pi Environment section below (or the
                General forwarded variables, which apply to every tool).
              </p>
            </template>

            <!-- Codex -->
            <template v-else-if="workerSubTab === 'codex'">
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
                Used when the foreman assigns a task to the Codex tool without an explicit model
                override.
              </p>
            </template>

            <!-- Claude: no per-tool default model here (the foreman's own LLM lives
                 in the Foreman tab); only its worker-CLI environment. -->
            <template v-else-if="workerSubTab === 'claude'">
              <p class="foreman-hint">
                The Claude worker CLI takes no default model here — set its override environment
                below.
              </p>
            </template>

            <!-- Per-tool override environment: passed ONLY to the selected tool's
                 runner, never to the other tools; overrides the General vars. -->
            <div v-if="workerSubTab !== 'general'" class="foreman-field">
              <label class="foreman-field-label">{{ activeToolLabel }} Overrides</label>
              <p class="foreman-hint">
                Passed only to the {{ activeToolLabel }} CLI — never to the other tools. Overrides a
                General forwarded variable for this tool. Pick a known variable or type your own.
              </p>
              <div class="env-var-list">
                <div v-for="row in toolEnvRows[workerSubTab]" :key="row.id" class="env-var-row">
                  <input
                    v-model="row.key"
                    class="settings-input env-var-key"
                    :list="`tool-env-keys-${workerSubTab}`"
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
                <datalist :id="`tool-env-keys-${workerSubTab}`">
                  <option v-for="k in TOOL_ENV_KEYS[workerSubTab]" :key="k" :value="k" />
                </datalist>
                <button class="pixel-btn env-var-add-btn" @click="addToolEnvVar">
                  + Add Variable
                </button>
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
import GuildSpawnProfiles from './GuildSpawnProfiles.vue'

const API_BASE = (import.meta.env.VITE_API_BASE as string) ?? ''

const emit = defineEmits<{ close: [] }>()

const guildStore = useGuildStore()
const ghStore = useGitHubStore()
const authStore = useAuthStore()

const currentGuild = computed(() => guildStore.currentGuild)

const TABS = [
  { id: 'general', label: 'General' },
  { id: 'foreman', label: 'Foreman' },
  { id: 'spawn', label: 'Worker Settings' },
  { id: 'members', label: 'Members' },
] as const
const activeTab = ref<(typeof TABS)[number]['id']>('general')

// Worker Settings sub-tabs: General (vars every tool receives) + one override
// tab per worker tool.
const WORKER_SUBTABS = [
  { id: 'general', label: 'General' },
  { id: 'claude', label: 'Claude' },
  { id: 'pi', label: 'Pi' },
  { id: 'codex', label: 'Codex' },
] as const
const workerSubTab = ref<(typeof WORKER_SUBTABS)[number]['id']>('general')
const activeToolLabel = computed(
  () => WORKER_SUBTABS.find((t) => t.id === workerSubTab.value)?.label ?? '',
)

// Common foreman env-var keys, surfaced as datalist suggestions on the Foreman
// tab. Users can still type any other key.
const FOREMAN_ENV_KEYS = [
  'ANTHROPIC_API_KEY',
  'ANTHROPIC_AUTH_TOKEN',
  'ANTHROPIC_BASE_URL',
  'AWS_DEFAULT_REGION',
  'AWS_PROFILE',
  'AWS_ACCESS_KEY_ID',
  'AWS_SECRET_ACCESS_KEY',
  'AWS_SESSION_TOKEN',
  'AWS_BEARER_TOKEN_BEDROCK',
]

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

// Masked foreman defaults supplied by the server's process environment (see
// GET foreman-config). Shown so an unset field reads as "inherited from env"
// rather than blank/unconfigured. Non-secret values (provider/model/region)
// come through in the clear; secrets are already masked server-side.
const envDefaults = ref<Record<string, string>>({})
const envDefaultKeys = computed(() => Object.keys(envDefaults.value))
const envDefaultsSummary = computed(() =>
  envDefaultKeys.value.map((k) => `${k}=${envDefaults.value[k]}`).join(', '),
)
const providerDefaultLabel = computed(() =>
  envDefaults.value.FOREMAN_PROVIDER
    ? `default (${envDefaults.value.FOREMAN_PROVIDER} · from env)`
    : 'default (anthropic)',
)
const foremanModelPlaceholder = computed(() => {
  const key = foremanProvider.value === 'bedrock' ? 'FOREMAN_BEDROCK_MODEL' : 'FOREMAN_MODEL'
  return envDefaults.value[key] ? `${envDefaults.value[key]} · from env` : 'default'
})
interface EnvVarRow {
  id: number
  key: string
  value: string
}
let envRowSeq = 0
// Guild env vars split by destination instead of a per-row `forward` checkbox:
// workerEnvRows reach every worker tool (persisted with forward=true), while
// foremanEnvRows stay with the foreman's own LLM (forward=false). The wire
// format still carries the flag; the UI derives it from which list a var is in.
const workerEnvRows = ref<EnvVarRow[]>([])
const foremanEnvRows = ref<EnvVarRow[]>([])

function addEnvRow(rows: EnvVarRow[]) {
  rows.push({ id: ++envRowSeq, key: '', value: '' })
}
function removeEnvRow(rows: EnvVarRow[], row: EnvVarRow) {
  const i = rows.indexOf(row)
  if (i >= 0) rows.splice(i, 1)
}

// Per-tool env var rows, keyed by tool id. Each tool's rows are saved under
// foreman_config.tool_env_vars[tool] and reach only that tool's runner.
const toolEnvRows = reactive<Record<string, EnvVarRow[]>>({ claude: [], pi: [], codex: [] })

function addToolEnvVar() {
  if (workerSubTab.value === 'general') return
  toolEnvRows[workerSubTab.value].push({ id: ++envRowSeq, key: '', value: '' })
}

function removeToolEnvVar(row: EnvVarRow) {
  if (workerSubTab.value === 'general') return
  const rows = toolEnvRows[workerSubTab.value]
  const i = rows.indexOf(row)
  if (i >= 0) rows.splice(i, 1)
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
      envDefaults.value = cfg.env_defaults ?? {}
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
      // Split by forward: forwarded → worker (General) list, rest → foreman list.
      workerEnvRows.value = []
      foremanEnvRows.value = []
      for (const e of (cfg.env_vars ?? []) as {
        key: string
        value?: string
        forward?: boolean
      }[]) {
        const target = e.forward ? workerEnvRows : foremanEnvRows
        target.value.push({ id: ++envRowSeq, key: e.key, value: e.value ?? '' })
      }
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
    // Combine both destination lists into the wire format: worker rows carry
    // forward=true, foreman-only rows forward=false. Skip empty keys; collapse
    // duplicate keys, keeping a non-empty value over a blank one. If the same
    // key appears in both lists, worker (forward=true) wins so it still reaches
    // workers.
    const envByKey = new Map<string, { value: string; forward: boolean }>()
    for (const { rows, forward } of [
      { rows: foremanEnvRows.value, forward: false },
      { rows: workerEnvRows.value, forward: true },
    ]) {
      for (const r of rows) {
        const key = r.key.trim()
        if (!key) continue
        const existing = envByKey.get(key)
        if (existing && r.value === '' && existing.value !== '') continue
        envByKey.set(key, { value: r.value, forward: forward || !!existing?.forward })
      }
    }
    body.env_vars = [...envByKey].map(([key, { value, forward }]) => ({ key, value, forward }))
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
      workerEnvRows.value = []
      foremanEnvRows.value = []
      for (const e of (saved.env_vars ?? []) as {
        key: string
        value?: string
        forward?: boolean
      }[]) {
        const target = e.forward ? workerEnvRows : foremanEnvRows
        target.value.push({ id: ++envRowSeq, key: e.key, value: e.value ?? '' })
      }
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
  githubAppInstallationId.value = currentGuild.value?.github_app_installation_id ?? ''
  savedInstallId.value = githubAppInstallationId.value
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
  if (appInstallStatusTimer) clearTimeout(appInstallStatusTimer)
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
  /* FactoryFloor robots/stations use z-index up to ~1000 (Math.round(y * 1000)),
     so the settings overlay must sit above that to stay clickable. */
  z-index: 2000;
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

.foreman-divider {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-brass);
  letter-spacing: 1px;
  text-transform: uppercase;
  border-top: 1px solid var(--color-brass-dark);
  padding-top: 12px;
  margin-top: 6px;
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

.foreman-hint,
.settings-hint {
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

.env-var-spacer {
  flex: 0 0 22px;
}

.env-var-head span {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-brass-dark);
  letter-spacing: 1px;
  text-transform: uppercase;
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
