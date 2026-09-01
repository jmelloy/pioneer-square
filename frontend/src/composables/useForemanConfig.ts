import { ref, computed, reactive, type ComputedRef } from 'vue'
import { useAuthStore } from '../stores/auth'
import { useModels } from './useModels'
import { loadSpawnPipeline, saveSpawnSettings } from './useSpawnPipeline'
import { normalizeEnvPairs, serializeSpawnSettings, toolDefaultsFrom } from './useSpawnSettings'
import { API_BASE, ApiError } from '../utils/api'

/** Pull the server's `detail` string off a failed response, falling back to
 *  the status code — the same shape `api()` puts on ApiError. */
async function _detail(res: Response): Promise<string> {
  try {
    return ((await res.json()) as { detail?: string }).detail ?? `HTTP ${res.status}`
  } catch {
    return `HTTP ${res.status}`
  }
}

export interface EnvVarRow {
  id: number
  key: string
  value: string
}

/**
 * Shared load/save state for a guild's settings dialogue: the foreman's own
 * orchestrator LLM settings (foreman_config) plus the guild's worker-facing
 * spawn baseline (spawn_settings). Two stores, two endpoints — the Foreman tab
 * and the Worker Settings tab share one instance, and `saveForemanConfig`
 * reports each half's outcome separately via `foremanConfigError` /
 * `workerSettingsError`.
 */
export function useForemanConfig(guildId: ComputedRef<string | undefined>) {
  const authStore = useAuthStore()
  const modelsStore = reactive(useModels())

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

  const foremanProviderModels = computed(() =>
    foremanProvider.value ? modelsStore.modelsForProvider(foremanProvider.value) : [],
  )
  const piDefaultModel = ref('')
  const piDefaultProvider = ref('')
  const piProviderModels = computed(() =>
    piDefaultProvider.value ? modelsStore.modelsForProvider(piDefaultProvider.value) : [],
  )
  const codexDefaultModel = ref('')
  const workerRepos = ref<string[]>([])
  const workerTools = ref<string[]>([])
  const foremanSystemSuffix = ref('')
  const foremanMaxRounds = ref<number | ''>('')
  const foremanPollMin = ref<number | ''>('')
  const foremanPollMax = ref<number | ''>('')
  const foremanSaving = ref(false)
  const foremanStatus = ref<'' | 'saved' | 'error'>('')
  // Which half of the save failed, if either. The two halves hit different
  // endpoints (foreman-config PATCH, spawn-settings PUT) and can fail
  // independently, so they report independently.
  const foremanConfigError = ref('')
  const workerSettingsError = ref('')
  let foremanStatusTimer: ReturnType<typeof setTimeout> | null = null

  // Masked foreman defaults supplied by the server's process environment (see
  // GET foreman-config). Shown read-only as fallback context; editable rows are
  // only values explicitly stored on the guild.
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

  let envRowSeq = 0
  // Guild env vars split by destination, which is also how they are stored:
  // workerEnvRows reach every worker tool (spawn_settings.env_vars), while
  // foremanEnvRows stay with the foreman's own LLM (foreman_config.env_vars).
  // There is no `forward` flag any more — the store a var lives in decides.
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
  // spawn_settings.tool_env_vars[tool] and reach only that tool's runner.
  const toolEnvRows = reactive<Record<string, EnvVarRow[]>>({ claude: [], pi: [], codex: [] })

  function addToolEnvVar(tool: string) {
    toolEnvRows[tool].push({ id: ++envRowSeq, key: '', value: '' })
  }

  function removeToolEnvVar(tool: string, row: EnvVarRow) {
    const rows = toolEnvRows[tool]
    const i = rows.indexOf(row)
    if (i >= 0) rows.splice(i, 1)
  }

  async function loadForemanConfig() {
    if (!guildId.value) return
    try {
      const res = await fetch(
        `${API_BASE}/api/guilds/${encodeURIComponent(guildId.value)}/foreman-config`,
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
        foremanSystemSuffix.value = cfg.system_prompt_suffix ?? ''
        foremanMaxRounds.value = cfg.max_rounds ?? ''
        foremanPollMin.value = cfg.poll_min_interval ?? ''
        foremanPollMax.value = cfg.poll_max_interval ?? ''
        foremanEnvRows.value = []
        for (const e of (cfg.env_vars ?? []) as {
          key: string
          value?: string
          forward?: boolean
        }[]) {
          if (!e.forward) {
            foremanEnvRows.value.push({ id: ++envRowSeq, key: e.key, value: e.value ?? '' })
          }
        }
      }

      // Worker-facing settings live in spawn_settings, not foreman_config. Use
      // the same spawn pipeline as User Preferences and the Launch surface so
      // model/env/default fields normalize identically everywhere.
      const workerCfg = (await loadSpawnPipeline(guildId.value)).settings
      if (workerCfg) {
        workerRepos.value = workerCfg.repos
        workerTools.value = workerCfg.tools
        const defaults = workerCfg.toolDefaults
        piDefaultModel.value = defaults.pi?.model ?? ''
        piDefaultProvider.value = defaults.pi?.provider ?? ''
        codexDefaultModel.value = defaults.codex?.model ?? ''
        workerEnvRows.value = workerCfg.envVars.map((e: { key: string; value?: string }) => ({
          id: ++envRowSeq,
          key: e.key,
          value: e.value ?? '',
        }))
        const toolEnv = workerCfg.toolEnvVars
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
    if (!guildId.value) return
    foremanSaving.value = true
    foremanStatus.value = ''
    foremanConfigError.value = ''
    workerSettingsError.value = ''
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
      // Foreman config now stores only foreman-only env vars. Worker-facing
      // env/defaults are saved to spawn_settings below.
      // Same empty-value rule as spawn settings; `forward: false` is sent
      // explicitly to clear the flag off any legacy row still carrying it.
      body.env_vars = normalizeEnvPairs(foremanEnvRows.value).map((p) => ({
        ...p,
        forward: false,
      }))
      const res = await fetch(
        `${API_BASE}/api/guilds/${encodeURIComponent(guildId.value)}/foreman-config`,
        {
          method: 'PATCH',
          headers: { ...authStore.authHeaders(), 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        },
      )
      // One serialisation path for every spawn-settings write (see
      // useSpawnSettings.serializeSpawnSettings) so blanking an env var here
      // does what it does in User Preferences and the Launch form.
      const workerBody = serializeSpawnSettings({
        repos: workerRepos.value,
        tools: workerTools.value,
        envVars: workerEnvRows.value,
        toolDefaults: toolDefaultsFrom({
          piProvider: piDefaultProvider.value,
          piModel: piDefaultModel.value,
          codexModel: codexDefaultModel.value,
        }),
        toolEnvVars: toolEnvRows,
      })
      let workerError = ''
      try {
        await saveSpawnSettings(guildId.value, workerBody)
      } catch (e) {
        workerError = e instanceof ApiError ? e.message : 'Failed to save worker settings'
      }
      // Report the halves separately: a successful foreman PATCH plus a failed
      // spawn-settings PUT used to surface as one undifferentiated "error" and
      // the user couldn't tell which landed (issue #1240).
      foremanConfigError.value = res.ok ? '' : await _detail(res)
      workerSettingsError.value = workerError
      if (res.ok && !workerError) {
        foremanStatus.value = 'saved'
        await loadForemanConfig()
      } else {
        foremanStatus.value = 'error'
      }
    } catch (e) {
      foremanStatus.value = 'error'
      foremanConfigError.value = e instanceof Error ? e.message : 'Failed to save'
    } finally {
      foremanSaving.value = false
      if (foremanStatusTimer) clearTimeout(foremanStatusTimer)
      foremanStatusTimer = setTimeout(() => {
        foremanStatus.value = ''
      }, 2000)
    }
  }

  function loadModels() {
    modelsStore.loadModels()
  }

  return {
    modelsStore,
    foremanModel,
    foremanProvider,
    onProviderChange,
    foremanProviderModels,
    piDefaultModel,
    piDefaultProvider,
    piProviderModels,
    codexDefaultModel,
    foremanSystemSuffix,
    foremanMaxRounds,
    foremanPollMin,
    foremanPollMax,
    foremanSaving,
    foremanStatus,
    foremanConfigError,
    workerSettingsError,
    envDefaults,
    envDefaultKeys,
    envDefaultsSummary,
    providerDefaultLabel,
    foremanModelPlaceholder,
    workerEnvRows,
    foremanEnvRows,
    toolEnvRows,
    addEnvRow,
    removeEnvRow,
    addToolEnvVar,
    removeToolEnvVar,
    loadModels,
    loadForemanConfig,
    saveForemanConfig,
  }
}

export type ForemanConfig = ReturnType<typeof useForemanConfig>
