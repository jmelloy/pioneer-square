<template>
  <div class="uws">
    <div v-if="!guildId" class="uws-hint">
      Open a guild to configure your worker settings for it.
    </div>
    <template v-else-if="loading">
      <div class="uws-hint">Loading…</div>
    </template>
    <template v-else>
      <p class="uws-intro">
        Your personal defaults for this guild — which repos and tools workers use for your tasks,
        and per-tool model/environment overrides. These apply only to you; guild owners set the
        shared baseline under Guild Settings → Worker Settings.
      </p>

      <label class="uws-sublabel">Repos</label>
      <div v-if="ghStore.repos.length === 0" class="uws-hint">
        No repos loaded — configure GitHub first.
      </div>
      <div v-else class="uws-repo-list">
        <template v-for="group in groupedRepos" :key="group.owner">
          <label
            class="uws-repo-row uws-org-row"
            :class="{ selected: orgAllSelected(group.owner) }"
          >
            <input
              type="checkbox"
              :ref="(el) => setOrgCheckboxRef(el as HTMLInputElement | null, group.owner)"
              :checked="orgAllSelected(group.owner)"
              @change="toggleOrg(group.owner)"
              class="uws-check"
            />
            <span class="uws-repo-name uws-org-name">{{ group.owner }}</span>
          </label>
          <label
            v-for="repo in group.repos"
            :key="repo.full_name"
            class="uws-repo-row uws-repo-indent"
            :class="{ selected: repos.includes(repo.full_name) }"
          >
            <input
              type="checkbox"
              :checked="repos.includes(repo.full_name)"
              @change="toggleRepo(repo.full_name)"
              class="uws-check"
            />
            <span class="uws-repo-name">{{ repo.full_name.split('/')[1] }}</span>
          </label>
        </template>
      </div>

      <label class="uws-sublabel">Tools</label>
      <div class="uws-tool-list">
        <label
          v-for="tool in AVAILABLE_TOOLS"
          :key="tool"
          class="uws-repo-row"
          :class="{ selected: tools.includes(tool) }"
        >
          <input
            type="checkbox"
            :checked="tools.includes(tool)"
            @change="toggleTool(tool)"
            class="uws-check"
          />
          <span class="uws-repo-name">{{ tool }}</span>
        </label>
      </div>

      <div class="uws-divider">Tool Overrides</div>
      <nav class="uws-tool-tabs">
        <button
          v-for="st in WORKER_SUBTABS"
          :key="st.id"
          type="button"
          class="uws-tool-tab"
          :class="{ active: subTab === st.id }"
          @click="subTab = st.id"
        >
          {{ st.label }}
        </button>
      </nav>

      <template v-if="subTab === 'general'">
        <p class="uws-field-hint">
          Every worker tool run on your behalf in this guild inherits these variables. Use the
          Claude / Pi / Codex tabs to override a value for a single tool.
        </p>
        <div class="env-var-list">
          <div v-for="row in workerEnvRows" :key="row.id" class="env-var-row">
            <input
              v-model="row.key"
              class="uws-input env-var-key"
              placeholder="KEY_NAME"
              spellcheck="false"
              autocomplete="off"
            />
            <input
              v-model="row.value"
              type="text"
              class="uws-input env-var-value"
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

      <template v-else-if="subTab === 'pi'">
        <div class="uws-field">
          <label class="uws-field-label">Provider Override</label>
          <select v-model="piDefaultProvider" class="uws-input">
            <option value="">default (Pi CLI)</option>
            <option v-for="p in modelsStore.providers" :key="p.id" :value="p.id">
              {{ p.name }}
            </option>
          </select>
        </div>
        <div class="uws-field">
          <label class="uws-field-label">Model Override</label>
          <input
            v-model="piDefaultModel"
            class="uws-input"
            list="uws-pi-model-hints"
            :placeholder="
              piDefaultProvider === 'bedrock' ? 'inference-profile ARN' : 'model override'
            "
            autocomplete="off"
          />
          <datalist id="uws-pi-model-hints">
            <option v-for="m in piProviderModels" :key="m.id" :value="m.id" :label="m.name" />
          </datalist>
        </div>
        <p class="uws-field-hint">
          Overrides the model Pi runs for your tasks when no explicit model/provider is assigned.
        </p>
      </template>

      <template v-else-if="subTab === 'codex'">
        <div class="uws-field">
          <label class="uws-field-label">Default Model</label>
          <input
            v-model="codexDefaultModel"
            class="uws-input"
            placeholder="e.g. gpt-5-codex"
            autocomplete="off"
          />
        </div>
        <p class="uws-field-hint">
          Used when Codex runs one of your tasks without an explicit model override.
        </p>
      </template>

      <template v-else-if="subTab === 'claude'">
        <p class="uws-field-hint">
          The Claude worker CLI takes no default model here — set its override environment below.
        </p>
      </template>

      <div v-if="subTab !== 'general'" class="uws-field">
        <label class="uws-field-label">{{ activeToolLabel }} Overrides</label>
        <p class="uws-field-hint">
          Passed only to the {{ activeToolLabel }} CLI on your tasks — overrides a General variable
          above for this tool.
        </p>
        <div class="env-var-list">
          <div v-for="row in toolEnvRows[subTab]" :key="row.id" class="env-var-row">
            <input
              v-model="row.key"
              class="uws-input env-var-key"
              placeholder="KEY_NAME"
              spellcheck="false"
              autocomplete="off"
            />
            <input
              v-model="row.value"
              type="text"
              class="uws-input env-var-value"
              placeholder="value"
              spellcheck="false"
              autocomplete="off"
            />
            <button
              class="env-var-delete-btn"
              @click="removeEnvRow(toolEnvRows[subTab], row)"
              title="Remove variable"
            >
              ✕
            </button>
          </div>
          <button class="pixel-btn env-var-add-btn" @click="addEnvRow(toolEnvRows[subTab])">
            + Add Variable
          </button>
        </div>
      </div>

      <div class="uws-divider">Repository Setup Commands</div>
      <p class="uws-field-hint">
        Shell commands (one per line) workers run in a repository before the agent starts, after
        mise tools are installed. Shared by everyone in this guild. Output appears in task logs.
      </p>
      <div class="uws-field">
        <label class="uws-field-label" for="uws-setup-repo">Repository</label>
        <select id="uws-setup-repo" v-model="setupRepo" class="uws-input" data-testid="setup-repo">
          <option value="">Select a repository…</option>
          <option v-for="r in setupRepoOptions" :key="r" :value="r">{{ r }}</option>
        </select>
      </div>
      <template v-if="setupRepo">
        <div v-if="setupLoading" class="uws-hint">Loading setup…</div>
        <template v-else>
          <div class="uws-field">
            <label class="uws-field-label" for="uws-setup-cmds">Commands</label>
            <textarea
              id="uws-setup-cmds"
              v-model="setupText"
              class="uws-input"
              rows="5"
              spellcheck="false"
              placeholder="npm ci"
              data-testid="setup-commands"
            />
          </div>
          <div class="uws-field">
            <label class="uws-field-label" for="uws-setup-timeout">
              Timeout per command (seconds)
            </label>
            <input
              id="uws-setup-timeout"
              v-model.number="setupTimeout"
              type="number"
              min="1"
              max="3600"
              class="uws-input"
              data-testid="setup-timeout"
            />
          </div>
          <p v-if="setupConfigured" class="uws-field-hint" data-testid="setup-current">
            Currently configured: {{ setupConfigured }} command(s).
          </p>
          <div class="uws-actions">
            <button
              class="pixel-btn"
              :disabled="setupSaving"
              data-testid="setup-save"
              @click="saveSetup"
            >
              {{ setupSaving ? 'Saving…' : 'Save Setup' }}
            </button>
            <button
              class="pixel-btn"
              :disabled="setupSaving || !setupConfigured"
              data-testid="setup-clear"
              @click="clearSetup"
            >
              Clear
            </button>
            <span v-if="setupStatus" class="save-status" :class="'save-status-' + setupStatus">
              {{ setupStatus === 'saved' ? 'Saved' : 'Error' }}
            </span>
          </div>
          <div v-if="setupError" class="uws-error" data-testid="setup-error">
            {{ setupError }}
          </div>
        </template>
      </template>

      <div class="uws-actions">
        <button class="pixel-btn uws-save-btn" :disabled="saving" @click="save">
          {{ saving ? 'Saving…' : 'Save' }}
        </button>
        <span v-if="status" class="save-status" :class="'save-status-' + status">
          {{ status === 'saved' ? 'Saved' : 'Error' }}
        </span>
      </div>
      <div v-if="errorMsg" class="uws-error">{{ errorMsg }}</div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { api, ApiError } from '../utils/api'
import { useGitHubStore } from '../stores/github'
import { useRepoSelection } from '../composables/useRepoSelection'
import { useModels } from '../composables/useModels'
import type { EnvVarRow } from '../composables/useForemanConfig'
import { SPAWN_TOOLS, loadSpawnPipeline, saveSpawnSettings } from '../composables/useSpawnPipeline'
import { serializeSpawnSettings, toolDefaultsFrom } from '../composables/useSpawnSettings'

const AVAILABLE_TOOLS = SPAWN_TOOLS

const WORKER_SUBTABS = [
  { id: 'general', label: 'General' },
  { id: 'claude', label: 'Claude' },
  { id: 'pi', label: 'Pi' },
  { id: 'codex', label: 'Codex' },
] as const

const props = defineProps<{ guildId?: string }>()

const ghStore = useGitHubStore()
const modelsStore = reactive(useModels())

const loading = ref(true)
const saving = ref(false)
const status = ref<'' | 'saved' | 'error'>('')
const errorMsg = ref('')

const repos = ref<string[]>([])
const tools = ref<string[]>([])
const subTab = ref<(typeof WORKER_SUBTABS)[number]['id']>('general')
const piDefaultProvider = ref('')
const piDefaultModel = ref('')
const codexDefaultModel = ref('')

let envRowSeq = 0
const workerEnvRows = ref<EnvVarRow[]>([])
const toolEnvRows = reactive<Record<string, EnvVarRow[]>>({ claude: [], pi: [], codex: [] })

// The repo/org/tool checkbox behaviour, shared with the Launch form and Guild
// Settings instead of copy-pasted into each (#1240).
const { groupedRepos, toggleRepo, orgAllSelected, toggleOrg, setOrgCheckboxRef, toggleTool } =
  useRepoSelection(repos, tools)

const activeToolLabel = computed(
  () => WORKER_SUBTABS.find((t) => t.id === subTab.value)?.label ?? '',
)
const piProviderModels = computed(() =>
  piDefaultProvider.value ? modelsStore.modelsForProvider(piDefaultProvider.value) : [],
)

let statusTimer: ReturnType<typeof setTimeout> | null = null

function addEnvRow(rows: EnvVarRow[]) {
  rows.push({ id: ++envRowSeq, key: '', value: '' })
}

function removeEnvRow(rows: EnvVarRow[], row: EnvVarRow) {
  const i = rows.indexOf(row)
  if (i >= 0) rows.splice(i, 1)
}

async function load() {
  if (!props.guildId) return
  loading.value = true
  try {
    if (ghStore.repos.length === 0 && ghStore.token) {
      await ghStore.fetchRepos().catch(() => {})
    }
    const cfg = (await loadSpawnPipeline(props.guildId)).settings
    repos.value = cfg?.repos ?? []
    tools.value = cfg?.tools ?? []
    const defaults = cfg?.toolDefaults ?? {}
    piDefaultProvider.value = defaults.pi?.provider ?? ''
    piDefaultModel.value = defaults.pi?.model ?? ''
    codexDefaultModel.value = defaults.codex?.model ?? ''
    workerEnvRows.value = (cfg?.envVars ?? []).map((e) => ({
      id: ++envRowSeq,
      key: e.key,
      value: e.value ?? '',
    }))
    const toolEnv = cfg?.toolEnvVars ?? {}
    for (const tool of ['claude', 'pi', 'codex']) {
      toolEnvRows[tool] = (toolEnv[tool] ?? []).map((e) => ({
        id: ++envRowSeq,
        key: e.key,
        value: e.value ?? '',
      }))
    }
  } catch {
    repos.value = []
    tools.value = []
  } finally {
    loading.value = false
  }
}

async function save() {
  if (!props.guildId) return
  saving.value = true
  status.value = ''
  errorMsg.value = ''
  try {
    // One serialisation path for every spawn-settings write, so blanking an env
    // var here does the same thing it does in the Launch form and Guild
    // Settings (see useSpawnSettings.serializeSpawnSettings).
    await saveSpawnSettings(
      props.guildId,
      serializeSpawnSettings({
        repos: repos.value,
        tools: tools.value,
        envVars: workerEnvRows.value,
        toolDefaults: toolDefaultsFrom({
          piProvider: piDefaultProvider.value,
          piModel: piDefaultModel.value,
          codexModel: codexDefaultModel.value,
        }),
        toolEnvVars: toolEnvRows,
      }),
    )
    status.value = 'saved'
  } catch (e) {
    status.value = 'error'
    errorMsg.value = e instanceof ApiError ? e.message : 'Failed to save'
  } finally {
    saving.value = false
    if (statusTimer) clearTimeout(statusTimer)
    statusTimer = setTimeout(() => {
      status.value = ''
    }, 2000)
  }
}

// ── Per-repository setup commands (guild-scoped, #1397) ──────────────────────
interface RepoSetup {
  setupCommands: string[]
  timeoutSeconds: number
}
const DEFAULT_SETUP_TIMEOUT = 600
const setupRepo = ref('')
const setupText = ref('')
const setupTimeout = ref(DEFAULT_SETUP_TIMEOUT)
const setupConfigured = ref(0)
const setupLoading = ref(false)
const setupSaving = ref(false)
const setupStatus = ref<'' | 'saved' | 'error'>('')
const setupError = ref('')

const setupRepoOptions = computed(() => {
  const names = new Set<string>(ghStore.repos.map((r) => r.full_name))
  for (const r of repos.value) names.add(r)
  return [...names].sort()
})

function setupUrl(repo: string) {
  return `/guilds/${encodeURIComponent(props.guildId ?? '')}/repos/${repo}/setup`
}

function applySetup(cfg: RepoSetup | undefined) {
  const cmds = cfg?.setupCommands ?? []
  setupText.value = cmds.join('\n')
  setupTimeout.value = cfg?.timeoutSeconds ?? DEFAULT_SETUP_TIMEOUT
  setupConfigured.value = cmds.length
}

async function loadSetup() {
  setupError.value = ''
  setupStatus.value = ''
  if (!props.guildId || !setupRepo.value) return
  const repo = setupRepo.value
  setupLoading.value = true
  try {
    const cfg = await api<RepoSetup>(setupUrl(repo))
    if (repo === setupRepo.value) applySetup(cfg)
  } catch (e) {
    if (repo === setupRepo.value) {
      applySetup(undefined)
      setupError.value = e instanceof ApiError ? e.message : 'Failed to load setup'
    }
  } finally {
    if (repo === setupRepo.value) setupLoading.value = false
  }
}

async function saveSetup() {
  if (!props.guildId || !setupRepo.value) return
  setupSaving.value = true
  setupStatus.value = ''
  setupError.value = ''
  try {
    const cfg = await api<RepoSetup>(setupUrl(setupRepo.value), {
      method: 'PUT',
      json: {
        setup_commands: setupText.value.split('\n'),
        timeout_seconds: setupTimeout.value,
      },
    })
    applySetup(cfg)
    setupStatus.value = 'saved'
  } catch (e) {
    setupStatus.value = 'error'
    setupError.value = e instanceof ApiError ? e.message : 'Failed to save setup'
  } finally {
    setupSaving.value = false
  }
}

async function clearSetup() {
  if (!props.guildId || !setupRepo.value) return
  setupSaving.value = true
  setupStatus.value = ''
  setupError.value = ''
  try {
    await api(setupUrl(setupRepo.value), { method: 'DELETE' })
    applySetup(undefined)
    setupStatus.value = 'saved'
  } catch (e) {
    setupStatus.value = 'error'
    setupError.value = e instanceof ApiError ? e.message : 'Failed to clear setup'
  } finally {
    setupSaving.value = false
  }
}

watch(setupRepo, () => loadSetup())

onMounted(() => {
  modelsStore.loadModels()
  load()
})

watch(
  () => props.guildId,
  () => {
    setupRepo.value = ''
    applySetup(undefined)
    load()
  },
)

onBeforeUnmount(() => {
  if (statusTimer) clearTimeout(statusTimer)
})
</script>

<style scoped>
.uws {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.uws-hint {
  font-size: var(--text-small);
  color: var(--color-text-dim);
  font-style: italic;
}

.uws-intro {
  font-size: var(--text-small);
  color: var(--color-text-dim);
  font-style: italic;
  margin: 0;
  line-height: var(--leading-body);
  overflow-wrap: anywhere;
}

.uws-sublabel {
  font: 600 var(--text-small) / var(--leading-heading) var(--font-ui);
  color: var(--color-teal);
  text-transform: uppercase;
  margin-top: 4px;
}

.uws-repo-list {
  display: flex;
  flex-direction: column;
  gap: 1px;
  max-height: 140px;
  overflow-y: auto;
  border: 1px solid var(--color-brass-dark);
  border-radius: 2px;
  background: var(--color-bg-secondary);
}

.uws-tool-list {
  display: flex;
  flex-wrap: wrap;
  flex-direction: row;
  gap: 2px;
  border: 1px solid var(--color-brass-dark);
  border-radius: 2px;
  background: var(--color-bg-secondary);
}

.uws-repo-row {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 5px 7px;
  cursor: pointer;
  border: 1px solid transparent;
  transition: background 0.1s;
}

.uws-repo-row:hover {
  background: var(--color-bg-tertiary);
}

.uws-repo-row.selected {
  background: rgba(232, 170, 0, 0.08);
  border-color: var(--color-brass-dark);
}

.uws-org-row {
  background: var(--color-bg-secondary);
  border-bottom: 1px solid var(--color-brass-dark);
}

.uws-org-row.selected {
  background: rgba(232, 170, 0, 0.12);
}

.uws-org-name {
  font: 600 var(--text-small) / var(--leading-heading) var(--font-ui);
  color: var(--color-teal);
}

.uws-repo-indent {
  padding-left: 22px;
}

.uws-check {
  accent-color: var(--color-brass);
  width: 16px;
  height: 16px;
  cursor: pointer;
  flex-shrink: 0;
}

.uws-repo-name {
  font-size: var(--text-small);
  color: var(--color-text);
  min-width: 0;
  overflow-wrap: anywhere;
}

.uws-divider {
  font: 600 var(--text-card-title) / var(--leading-heading) var(--font-ui);
  color: var(--color-brass);
  text-transform: uppercase;
  border-top: 1px solid var(--color-brass-dark);
  padding-top: 12px;
  margin-top: 6px;
}

.uws-tool-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-bottom: 2px;
}

.uws-tool-tab {
  background: none;
  border: 1px solid var(--color-brass-dark);
  color: var(--color-text-dim);
  cursor: pointer;
  font: 600 var(--text-small) / var(--leading-heading) var(--font-pixel);
  text-transform: uppercase;
  padding: 6px 12px;
  border-radius: 2px;
  transition:
    color 0.12s,
    background 0.12s,
    border-color 0.12s;
}

.uws-tool-tab:hover {
  color: var(--color-brass);
  background: rgba(232, 170, 0, 0.06);
}

.uws-tool-tab.active {
  color: var(--color-brass-light);
  background: rgba(232, 170, 0, 0.1);
  border-color: var(--color-brass);
}

.uws-field {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.uws-field-label {
  font: 600 var(--text-small) / var(--leading-heading) var(--font-ui);
  color: var(--color-text-dim);
  text-transform: uppercase;
}

.uws-field-hint {
  font-size: var(--text-small);
  color: var(--color-text-dim);
  font-style: italic;
  margin: 0 0 3px;
  line-height: var(--leading-body);
  overflow-wrap: anywhere;
}

.uws-input {
  flex: 1;
  background: var(--color-bg);
  border: 1px solid var(--color-brass-dark);
  color: var(--color-text);
  font: var(--text-input) / 1.5 var(--font-ui);
  padding: 7px;
  border-radius: 2px;
  min-width: 0;
}

.uws-input:focus {
  border-color: var(--color-brass);
}

.uws-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 2px;
}

.uws-save-btn {
  padding: 5px 9px;
}

.uws-save-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.uws-error {
  font-size: var(--text-small);
  color: var(--color-red);
  word-break: break-word;
}

@media (max-width: 700px) {
  .uws-repo-row {
    min-height: 44px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .uws-tool-tab {
    transition: none;
  }
}
</style>
