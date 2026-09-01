import { describe, it, expect } from 'vitest'
import {
  normalizeEnvPairs,
  resolveSpawnLayers,
  serializeSpawnSettings,
  toolDefaultsFrom,
  type SpawnLayers,
} from '../useSpawnSettings'
import type { SpawnSettings, GuildSpawnDefaults } from '../useSpawnPipeline'

function guild(over: Partial<GuildSpawnDefaults> = {}): GuildSpawnDefaults {
  return { repos: [], tools: [], agent_count: null, ...over }
}

function user(over: Partial<SpawnSettings> = {}): SpawnSettings {
  return { repos: [], tools: [], envVars: [], toolDefaults: {}, toolEnvVars: {}, ...over }
}

function layers(over: Partial<SpawnLayers> = {}): SpawnLayers {
  return { guild: null, user: null, draft: null, ...over }
}

describe('resolveSpawnLayers — precedence', () => {
  it('returns an empty config when every layer is missing', () => {
    const { resolved, source } = resolveSpawnLayers(layers())
    expect(resolved).toEqual({
      repos: [],
      tools: [],
      agentCount: null,
      envVars: [],
      toolDefaults: {},
      toolEnvVars: {},
    })
    expect(source).toBe('custom')
  })

  it('uses guild defaults when the user has saved nothing', () => {
    const { resolved, source } = resolveSpawnLayers(
      layers({ guild: guild({ repos: ['a/b'], tools: ['claude'], agent_count: 2 }) }),
    )
    expect(resolved.repos).toEqual(['a/b'])
    expect(resolved.tools).toEqual(['claude'])
    expect(resolved.agentCount).toBe(2)
    expect(source).toBe('guild')
  })

  it('lets user settings replace — not concatenate — the guild lists', () => {
    const { resolved, source } = resolveSpawnLayers(
      layers({
        guild: guild({ repos: ['a/b', 'a/c'], tools: ['claude'] }),
        user: user({ repos: ['x/y'], tools: ['pi'] }),
      }),
    )
    expect(resolved.repos).toEqual(['x/y'])
    expect(resolved.tools).toEqual(['pi'])
    expect(source).toBe('saved')
  })

  it('falls back to the guild list per-field when the user list is empty', () => {
    const { resolved } = resolveSpawnLayers(
      layers({
        guild: guild({ repos: ['a/b'], tools: ['claude'], agent_count: 4 }),
        // repos saved, tools never set
        user: user({ repos: ['x/y'] }),
      }),
    )
    expect(resolved.repos).toEqual(['x/y'])
    expect(resolved.tools).toEqual(['claude'])
    // agentCount only ever comes from the guild — the user row has no such field
    expect(resolved.agentCount).toBe(4)
  })

  it('puts this-launch edits on top of both', () => {
    const { resolved, source } = resolveSpawnLayers(
      layers({
        guild: guild({ repos: ['a/b'] }),
        user: user({ repos: ['x/y'] }),
        draft: { repos: ['draft/repo'] },
      }),
    )
    expect(resolved.repos).toEqual(['draft/repo'])
    expect(source).toBe('custom')
  })

  it('overlays env vars key-by-key rather than replacing the map', () => {
    const { resolved } = resolveSpawnLayers(
      layers({
        user: user({
          envVars: [
            { key: 'SHARED', value: 'user' },
            { key: 'ONLY_USER', value: 'u' },
          ],
        }),
        draft: { envVars: [{ key: 'SHARED', value: 'draft' }] },
      }),
    )
    expect(resolved.envVars).toEqual([
      { key: 'SHARED', value: 'draft' },
      { key: 'ONLY_USER', value: 'u' },
    ])
  })

  it('never lets an empty value from a higher layer blank a real one below', () => {
    const { resolved } = resolveSpawnLayers(
      layers({
        user: user({ envVars: [{ key: 'TOKEN', value: 'real' }] }),
        draft: { envVars: [{ key: 'TOKEN', value: '' }] },
      }),
    )
    expect(resolved.envVars).toEqual([{ key: 'TOKEN', value: 'real' }])
  })

  it('applies the same guard to per-tool env and tool defaults', () => {
    const { resolved } = resolveSpawnLayers(
      layers({
        user: user({
          toolDefaults: { pi: { provider: 'bedrock', model: 'arn:x' } },
          toolEnvVars: { claude: [{ key: 'CLAUDE_TOK', value: 'real' }] },
        }),
        draft: {
          toolDefaults: { pi: { model: '' } },
          toolEnvVars: { claude: [{ key: 'CLAUDE_TOK', value: '' }] },
        },
      }),
    )
    expect(resolved.toolDefaults.pi).toEqual({ provider: 'bedrock', model: 'arn:x' })
    expect(resolved.toolEnvVars.claude).toEqual([{ key: 'CLAUDE_TOK', value: 'real' }])
  })

  it('reports source=guild when the user row exists but is empty', () => {
    const { source } = resolveSpawnLayers(
      layers({ guild: guild({ repos: ['a/b'] }), user: user() }),
    )
    expect(source).toBe('guild')
  })
})

describe('normalizeEnvPairs — the one empty-value rule', () => {
  it('drops blank keys and trims the rest', () => {
    expect(
      normalizeEnvPairs([
        { key: '  ', value: 'orphan' },
        { key: ' KEY ', value: 'v' },
      ]),
    ).toEqual([{ key: 'KEY', value: 'v' }])
  })

  it('keeps a blank value — it is "unset at my layer", not "delete this key"', () => {
    expect(normalizeEnvPairs([{ key: 'K', value: '' }])).toEqual([{ key: 'K', value: '' }])
  })

  it('collapses duplicate keys preferring the non-empty value, either order', () => {
    expect(
      normalizeEnvPairs([
        { key: 'K', value: '' },
        { key: 'K', value: 'real' },
      ]),
    ).toEqual([{ key: 'K', value: 'real' }])
    expect(
      normalizeEnvPairs([
        { key: 'K', value: 'real' },
        { key: 'K', value: '' },
      ]),
    ).toEqual([{ key: 'K', value: 'real' }])
  })

  it('keeps values verbatim — whitespace can be significant in a token', () => {
    expect(normalizeEnvPairs([{ key: 'K', value: ' padded ' }])).toEqual([
      { key: 'K', value: ' padded ' },
    ])
  })
})

describe('serializeSpawnSettings', () => {
  it('emits every tool key so a cleared tool actually clears server-side', () => {
    const body = serializeSpawnSettings({ toolEnvVars: { claude: [{ key: 'A', value: '1' }] } })
    expect(Object.keys(body.toolEnvVars).sort()).toEqual(['claude', 'codex', 'pi'])
    expect(body.toolEnvVars.pi).toEqual([])
  })

  it('drops tool defaults whose fields are all empty', () => {
    expect(
      serializeSpawnSettings({
        toolDefaults: { pi: { provider: '', model: '' }, codex: { model: 'gpt-5' } },
      }).toolDefaults,
    ).toEqual({ codex: { model: 'gpt-5' } })
  })

  it('applies the shared env rule to both shared and per-tool pairs', () => {
    const body = serializeSpawnSettings({
      envVars: [
        { key: '', value: 'dropped' },
        { key: 'A', value: '' },
        { key: 'A', value: 'real' },
      ],
      toolEnvVars: { pi: [{ key: ' B ', value: 'x' }] },
    })
    expect(body.envVars).toEqual([{ key: 'A', value: 'real' }])
    expect(body.toolEnvVars.pi).toEqual([{ key: 'B', value: 'x' }])
  })

  it('does not mutate the draft it was handed', () => {
    const repos = ['a/b']
    const body = serializeSpawnSettings({ repos })
    body.repos.push('c/d')
    expect(repos).toEqual(['a/b'])
  })
})

describe('toolDefaultsFrom', () => {
  it('omits a tool entirely when its fields are blank', () => {
    expect(toolDefaultsFrom({ piProvider: '', piModel: '', codexModel: '' })).toEqual({})
  })

  it('includes only the fields that were set', () => {
    expect(toolDefaultsFrom({ piProvider: 'bedrock', codexModel: 'gpt-5' })).toEqual({
      pi: { provider: 'bedrock' },
      codex: { model: 'gpt-5' },
    })
  })
})
