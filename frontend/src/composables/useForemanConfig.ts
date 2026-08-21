import { ref, computed, reactive, type ComputedRef } from 'vue'
import { useAuthStore } from '../stores/auth'
import { useModels } from './useModels'

const API_BASE = (import.meta.env.VITE_API_BASE as string) ?? ''

export interface EnvVarRow {
  id: number
  key: string
  value: string
  inherited?: boolean
}

/**
 * Shared load/save state for a guild's foreman-config: the foreman's own
 * orchestrator LLM settings plus the per-worker-tool defaults/env vars. Both
 * the Foreman tab and the Worker Settings tab read and write this same
 * config, so the panel creates one instance and passes it to both.
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

  function addInheritedForemanEnvRows() {
    const existing = new Set(foremanEnvRows.value.map((r) => r.key))
    for (const key of envDefaultKeys.value) {
      if (!existing.has(key)) {
        foremanEnvRows.value.push({ id: ++envRowSeq, key, value: '', inherited: true })
      }
    }
  }
  function removeEnvRow(rows: EnvVarRow[], row: EnvVarRow) {
    const i = rows.indexOf(row)
    if (i >= 0) rows.splice(i, 1)
  }

  // Per-tool env var rows, keyed by tool id. Each tool's rows are saved under
  // foreman_config.tool_env_vars[tool] and reach only that tool's runner.
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
        addInheritedForemanEnvRows()
      }

      // Worker-facing settings live in spawn_settings, not foreman_config, so
      // credentials/default models are visible here for editing/deleting.
      const workerRes = await fetch(
        `${API_BASE}/api/guilds/${encodeURIComponent(guildId.value)}/spawn-settings`,
        { headers: authStore.authHeaders() },
      )
      if (workerRes.ok) {
        const cfg = await workerRes.json()
        workerRepos.value = cfg.repos ?? []
        workerTools.value = cfg.tools ?? []
        const defaults = cfg.toolDefaults ?? {}
        piDefaultModel.value = defaults.pi?.model ?? cfg.model ?? ''
        piDefaultProvider.value = defaults.pi?.provider ?? cfg.provider ?? ''
        codexDefaultModel.value = defaults.codex?.model ?? ''
        workerEnvRows.value = (cfg.envVars ?? []).map((e: { key: string; value?: string }) => ({
          id: ++envRowSeq,
          key: e.key,
          value: e.value ?? '',
        }))
        const toolEnv = cfg.toolEnvVars ?? {}
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
      const envByKey = new Map<string, string>()
      for (const r of foremanEnvRows.value) {
        const key = r.key.trim()
        if (!key) continue
        if (r.inherited && r.value === '') continue
        const existing = envByKey.get(key)
        if (existing && r.value === '' && existing !== '') continue
        envByKey.set(key, r.value)
      }
      body.env_vars = [...envByKey].map(([key, value]) => ({ key, value, forward: false }))
      const res = await fetch(
        `${API_BASE}/api/guilds/${encodeURIComponent(guildId.value)}/foreman-config`,
        {
          method: 'PATCH',
          headers: { ...authStore.authHeaders(), 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        },
      )
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
      const toolDefaults: Record<string, Record<string, string>> = {}
      if (piDefaultProvider.value || piDefaultModel.value) {
        toolDefaults.pi = {}
        if (piDefaultProvider.value) toolDefaults.pi.provider = piDefaultProvider.value
        if (piDefaultModel.value) toolDefaults.pi.model = piDefaultModel.value
      }
      if (codexDefaultModel.value) toolDefaults.codex = { model: codexDefaultModel.value }
      const workerBody = {
        repos: workerRepos.value,
        tools: workerTools.value,
        envVars: workerEnvRows.value
          .filter((r) => r.key.trim())
          .map((r) => ({ key: r.key.trim(), value: r.value })),
        provider: null,
        model: null,
        toolDefaults,
        toolEnvVars,
      }
      const workerRes = await fetch(
        `${API_BASE}/api/guilds/${encodeURIComponent(guildId.value)}/spawn-settings`,
        {
          method: 'PUT',
          headers: { ...authStore.authHeaders(), 'Content-Type': 'application/json' },
          body: JSON.stringify(workerBody),
        },
      )
      if (res.ok && workerRes.ok) {
        foremanStatus.value = 'saved'
        await loadForemanConfig()
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
