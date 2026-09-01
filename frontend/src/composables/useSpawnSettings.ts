/**
 * The frontend counterpart to backend/spawn_config.py: one place that layers
 * guild defaults under this user's saved settings under their this-launch
 * edits, and one place that serialises the result for the wire.
 *
 * Before this module existed the override chain was rebuilt by hand in every
 * consumer and the three copies of the wire-serialisation block disagreed on
 * what an empty value meant, so blanking an env var did something different in
 * User Preferences, the Launch form and Guild Settings (issue #1240).
 *
 * Precedence, low -> high: guild defaults < user settings < this-launch draft.
 * Merge rules mirror `spawn_config.merge_layers` exactly:
 *   - lists (repos, tools): the highest layer with a NON-EMPTY list wins
 *     (replace, not concatenate).
 *   - scalars (agentCount): the highest layer with a non-null value.
 *   - maps (envVars, toolDefaults, toolEnvVars): key-by-key overlay, higher
 *     layer wins — but an empty value never blanks a real value from a lower
 *     layer. That guard lives here and nowhere else.
 *
 * The guild layer is only what the client can actually see: /spawn-defaults
 * (repos/tools/agent_count). A guild's env vars reach the browser masked, via
 * /spawn-credentials, so they are deliberately not resolvable here — the
 * backend layers the real values at spawn time.
 */
import { computed, ref, type Ref } from 'vue'
import {
  SPAWN_TOOLS,
  loadSpawnPipeline,
  saveSpawnSettings,
  type EnvPair,
  type GuildSpawnDefaults,
  type SpawnCredentials,
  type SpawnSettings,
  type ToolDefault,
} from './useSpawnPipeline'
import { api } from '../utils/api'

/** The effective launch config: what this user would actually launch with. */
export interface ResolvedSpawn {
  repos: string[]
  tools: string[]
  agentCount: number | null
  envVars: EnvPair[]
  toolDefaults: Record<string, ToolDefault>
  toolEnvVars: Record<string, EnvPair[]>
}

/** A partial layer. A field left undefined (or an empty list) means "unset here". */
export type SpawnDraft = Partial<ResolvedSpawn>

export interface SpawnLayers {
  guild: GuildSpawnDefaults | null
  user: SpawnSettings | null
  draft: SpawnDraft | null
}

/** Which layer the resolved config came from — drives the "showing guild
 *  defaults" affordance in the launch form. */
export type SpawnSource = 'guild' | 'saved' | 'custom'

export function emptyResolvedSpawn(): ResolvedSpawn {
  return { repos: [], tools: [], agentCount: null, envVars: [], toolDefaults: {}, toolEnvVars: {} }
}

/**
 * The single empty-value rule for env pairs, applied everywhere pairs are
 * edited or sent.
 *
 * - A pair with a blank key is dropped: it sets nothing.
 * - Keys are trimmed; values are kept verbatim (leading/trailing whitespace can
 *   be significant in a token or a path).
 * - Duplicate keys collapse to one, preferring a non-empty value, so a stray
 *   blank row can't shadow the real value the user typed elsewhere in the form.
 * - A blank value with no non-blank sibling is KEPT, not dropped. It is the
 *   user saying "this key, empty at my layer", and `mergeEnv` (here) and
 *   `spawn_config._merge_env` (server) both refuse to let it blank a real value
 *   from a lower layer. Dropping the row instead would silently delete the key.
 */
export function normalizeEnvPairs(pairs: readonly EnvPair[] | undefined): EnvPair[] {
  const byKey = new Map<string, string>()
  for (const p of pairs ?? []) {
    const key = p.key.trim()
    if (!key) continue
    const value = p.value ?? ''
    const existing = byKey.get(key)
    if (existing !== undefined && value === '' && existing !== '') continue
    byKey.set(key, value)
  }
  return [...byKey].map(([key, value]) => ({ key, value }))
}

/** Overlay `over` onto `base`; an empty value never blanks a real one. */
function mergeEnv(base: EnvPair[], over: EnvPair[]): EnvPair[] {
  const out = new Map(base.map((p) => [p.key, p.value]))
  for (const { key, value } of over) {
    if (value === '' && out.get(key)) continue
    out.set(key, value)
  }
  return [...out].map(([key, value]) => ({ key, value }))
}

function mergeToolDefaults(
  base: Record<string, ToolDefault>,
  over: Record<string, ToolDefault> | undefined,
): Record<string, ToolDefault> {
  const out: Record<string, ToolDefault> = { ...base }
  for (const [tool, defaults] of Object.entries(over ?? {})) {
    const merged: ToolDefault = { ...(out[tool] ?? {}) }
    for (const [k, v] of Object.entries(defaults ?? {}) as [keyof ToolDefault, string][]) {
      if (!v && merged[k]) continue
      merged[k] = v
    }
    out[tool] = merged
  }
  return out
}

function mergeToolEnv(
  base: Record<string, EnvPair[]>,
  over: Record<string, EnvPair[]> | undefined,
): Record<string, EnvPair[]> {
  const out: Record<string, EnvPair[]> = { ...base }
  for (const [tool, pairs] of Object.entries(over ?? {})) {
    out[tool] = mergeEnv(out[tool] ?? [], normalizeEnvPairs(pairs))
  }
  return out
}

/**
 * Fold the layers into the config this user would launch with. Pure: same
 * layers in, same resolution out — no component, no fetch, no store.
 */
export function resolveSpawnLayers(layers: SpawnLayers): {
  resolved: ResolvedSpawn
  source: SpawnSource
} {
  const r = emptyResolvedSpawn()
  let source: SpawnSource = 'custom'

  const apply = (layer: SpawnDraft | null | undefined, tag: SpawnSource) => {
    if (!layer) return
    let touched = false
    if (layer.repos?.length) {
      r.repos = [...layer.repos]
      touched = true
    }
    if (layer.tools?.length) {
      r.tools = [...layer.tools]
      touched = true
    }
    if (layer.agentCount != null) {
      r.agentCount = layer.agentCount
      touched = true
    }
    if (layer.envVars?.length) {
      r.envVars = mergeEnv(r.envVars, normalizeEnvPairs(layer.envVars))
      touched = true
    }
    if (layer.toolDefaults && Object.keys(layer.toolDefaults).length) {
      r.toolDefaults = mergeToolDefaults(r.toolDefaults, layer.toolDefaults)
      touched = true
    }
    if (layer.toolEnvVars && Object.values(layer.toolEnvVars).some((p) => p?.length)) {
      r.toolEnvVars = mergeToolEnv(r.toolEnvVars, layer.toolEnvVars)
      touched = true
    }
    if (touched) source = tag
  }

  apply(
    layers.guild && {
      repos: layers.guild.repos,
      tools: layers.guild.tools,
      agentCount: layers.guild.agent_count,
    },
    'guild',
  )
  apply(layers.user, 'saved')
  apply(layers.draft, 'custom')
  return { resolved: r, source }
}

/**
 * The one wire-serialisation block. Every PUT to /spawn-settings goes through
 * here so the three surfaces that write spawn settings agree on what they send.
 * Empty tool defaults are dropped (the server drops them too); env pairs follow
 * `normalizeEnvPairs`.
 */
export function serializeSpawnSettings(draft: SpawnDraft): SpawnSettings {
  const toolDefaults: Record<string, ToolDefault> = {}
  for (const [tool, defaults] of Object.entries(draft.toolDefaults ?? {})) {
    const kept = Object.fromEntries(Object.entries(defaults ?? {}).filter(([, v]) => v))
    if (Object.keys(kept).length) toolDefaults[tool] = kept
  }
  return {
    repos: [...(draft.repos ?? [])],
    tools: [...(draft.tools ?? [])],
    envVars: normalizeEnvPairs(draft.envVars),
    toolDefaults,
    toolEnvVars: Object.fromEntries(
      SPAWN_TOOLS.map((tool) => [tool, normalizeEnvPairs(draft.toolEnvVars?.[tool])]),
    ),
  }
}

/** Build the per-tool default map from the flat fields every settings form uses. */
export function toolDefaultsFrom(fields: {
  piProvider?: string
  piModel?: string
  codexModel?: string
}): Record<string, ToolDefault> {
  const out: Record<string, ToolDefault> = {}
  if (fields.piProvider || fields.piModel) {
    out.pi = {}
    if (fields.piProvider) out.pi.provider = fields.piProvider
    if (fields.piModel) out.pi.model = fields.piModel
  }
  if (fields.codexModel) out.codex = { model: fields.codexModel }
  return out
}

/**
 * Load, resolve and save one guild's spawn settings.
 *
 * `resolved` is the effective launch config; `layers` exposes the individual
 * layers so a form can say "showing the guild defaults". Consumers become forms
 * over `resolved` instead of re-deriving the precedence chain each time.
 */
export function useSpawnSettings(guildId: Ref<string | undefined>) {
  const guild = ref<GuildSpawnDefaults | null>(null)
  const user = ref<SpawnSettings | null>(null)
  const credentials = ref<SpawnCredentials | null>(null)
  const draft = ref<SpawnDraft | null>(null)
  // Set by resetToGuild(): suppress the user layer so the guild baseline shows
  // through. restoreSaved() puts it back — nothing is deleted server-side.
  const ignoreUserLayer = ref(false)
  const loading = ref(false)
  const loaded = ref(false)

  const layers = computed<SpawnLayers>(() => ({
    guild: guild.value,
    // resetToGuild() suppresses only what the guild baseline can actually
    // supply — repos, tools, agent count. A user's env vars and tool defaults
    // are their own (the guild's are separate, and reach the browser masked),
    // so resetting the launch shape must not silently drop them.
    user:
      ignoreUserLayer.value && user.value ? { ...user.value, repos: [], tools: [] } : user.value,
    draft: draft.value,
  }))
  const resolution = computed(() => resolveSpawnLayers(layers.value))
  const resolved = computed(() => resolution.value.resolved)
  const source = computed<SpawnSource>(() => {
    // An explicit reset shows the guild baseline even when the user's env vars
    // are still layered underneath the form.
    if (draft.value) return 'custom'
    return ignoreUserLayer.value ? 'guild' : resolution.value.source
  })

  const hasGuildDefaults = computed(
    () =>
      !!guild.value &&
      ((guild.value.repos?.length ?? 0) > 0 ||
        (guild.value.tools?.length ?? 0) > 0 ||
        guild.value.agent_count != null),
  )
  /** True when this user's row carries anything worth restoring — drives the
   *  "restore my settings" affordance. */
  const hasSavedSettings = computed(() => {
    const s = user.value
    if (!s) return false
    return !!(
      s.repos?.length ||
      s.tools?.length ||
      s.envVars?.length ||
      Object.values(s.toolDefaults ?? {}).some((d) => d.provider || d.model) ||
      Object.values(s.toolEnvVars ?? {}).some((p) => p.length)
    )
  })

  async function load() {
    if (!guildId.value) return
    loading.value = true
    try {
      const pipeline = await loadSpawnPipeline(guildId.value)
      guild.value = pipeline.defaults
      user.value = pipeline.settings
      credentials.value = pipeline.credentials
      draft.value = null
      ignoreUserLayer.value = false
      loaded.value = true
    } finally {
      loading.value = false
    }
  }

  /** Record this-launch edits. Passing null clears them. */
  function setDraft(next: SpawnDraft | null) {
    draft.value = next
  }

  /** Show the guild's launch shape (repos/tools/agent count) instead of this
   *  user's. Nothing is deleted server-side; restoreSaved() undoes it. */
  function resetToGuild() {
    ignoreUserLayer.value = true
    draft.value = null
  }

  function restoreSaved() {
    ignoreUserLayer.value = false
    draft.value = null
  }

  /**
   * Persist `settings` to the user's row ('user') or the guild baseline
   * ('guild'). Both scopes serialise identically; only the endpoint differs.
   */
  async function save(settings: SpawnDraft, scope: 'user' | 'guild' = 'user') {
    if (!guildId.value) return
    const body = serializeSpawnSettings(settings)
    if (scope === 'user') {
      user.value = await saveSpawnSettings(guildId.value, body)
      ignoreUserLayer.value = false
    } else {
      guild.value = await api<GuildSpawnDefaults>(
        `/guilds/${encodeURIComponent(guildId.value)}/spawn-defaults`,
        {
          method: 'PUT',
          json: { repos: body.repos, tools: body.tools, agent_count: settings.agentCount ?? null },
        },
      )
    }
    draft.value = null
  }

  return {
    layers,
    resolved,
    source,
    credentials,
    loading,
    loaded,
    hasGuildDefaults,
    hasSavedSettings,
    load,
    setDraft,
    resetToGuild,
    restoreSaved,
    save,
  }
}
