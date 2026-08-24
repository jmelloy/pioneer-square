import { api } from '../utils/api'

export const SPAWN_TOOLS = ['claude', 'codex', 'pi'] as const
export type SpawnTool = (typeof SPAWN_TOOLS)[number]

export interface EnvPair {
  key: string
  value: string
}

export interface ToolDefault {
  provider?: string
  model?: string
}

export interface SpawnSettings {
  repos: string[]
  tools: string[]
  envVars: EnvPair[]
  toolDefaults: Record<string, ToolDefault>
  toolEnvVars: Record<string, EnvPair[]>
}

export interface GuildSpawnDefaults {
  repos: string[]
  tools: string[]
  agent_count: number | null
}

export interface GuildEnvVarStatus {
  key: string
  masked_value: string
}

export interface SpawnCredentials {
  guild_env_vars: GuildEnvVarStatus[]
  guild_tool_env_vars?: Record<string, GuildEnvVarStatus[]>
}

export interface SpawnPipeline {
  settings: SpawnSettings | null
  defaults: GuildSpawnDefaults | null
  credentials: SpawnCredentials | null
}

export function emptySpawnSettings(): SpawnSettings {
  return { repos: [], tools: [], envVars: [], toolDefaults: {}, toolEnvVars: {} }
}

export function normalizeSpawnSettings(
  cfg: Partial<SpawnSettings> | null | undefined,
): SpawnSettings {
  return {
    repos: cfg?.repos ?? [],
    tools: cfg?.tools ?? [],
    envVars: (cfg?.envVars ?? []).map((p) => ({ key: p.key, value: p.value ?? '' })),
    toolDefaults: cfg?.toolDefaults ?? {},
    toolEnvVars: Object.fromEntries(
      SPAWN_TOOLS.map((tool) => [
        tool,
        (cfg?.toolEnvVars?.[tool] ?? []).map((p) => ({ key: p.key, value: p.value ?? '' })),
      ]),
    ),
  }
}

export async function loadSpawnPipeline(guildId: string): Promise<SpawnPipeline> {
  const enc = encodeURIComponent(guildId)
  const [settings, defaults, credentials] = await Promise.all([
    api<Partial<SpawnSettings>>(`/guilds/${enc}/spawn-settings`)
      .then(normalizeSpawnSettings)
      .catch(() => null),
    api<GuildSpawnDefaults>(`/guilds/${enc}/spawn-defaults`).catch(() => null),
    api<SpawnCredentials>(`/guilds/${enc}/spawn-credentials`).catch(() => null),
  ])
  return { settings, defaults, credentials }
}

export async function saveSpawnSettings(
  guildId: string,
  settings: SpawnSettings,
): Promise<SpawnSettings> {
  return normalizeSpawnSettings(
    await api<Partial<SpawnSettings>>(`/guilds/${encodeURIComponent(guildId)}/spawn-settings`, {
      method: 'PUT',
      json: settings,
    }),
  )
}
