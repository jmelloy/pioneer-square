import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import GuildSettingsPanel from '../GuildSettingsPanel.vue'
import { useGuildStore } from '../../stores/guild'
import { useAuthStore } from '../../stores/auth'

// Placeholder account id — the test only needs a bedrock-shaped ARN string.
const BEDROCK_ARN =
  'arn:aws:bedrock:us-east-1:000000000000:inference-profile/us.anthropic.claude-sonnet-4-6'

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response
}

// Route fetches by URL so the test doesn't depend on call ordering (the
// useModels composable caches its result across calls).
function mockFetch(
  foremanConfig: Record<string, unknown>,
  workerConfig: Record<string, unknown> = {},
) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/models')) {
      return Promise.resolve(
        jsonResponse([
          { id: 'bedrock', name: 'Bedrock', models: [] },
          { id: 'anthropic', name: 'Anthropic', models: [] },
        ]),
      )
    }
    if (url.includes('/foreman-config')) return Promise.resolve(jsonResponse(foremanConfig))
    if (url.includes('/spawn-settings')) return Promise.resolve(jsonResponse(workerConfig))
    return Promise.resolve(jsonResponse({}))
  })
}

async function openTab(
  label: string,
  foremanConfig: Record<string, unknown>,
  workerConfig: Record<string, unknown> = {},
) {
  mockFetch(foremanConfig, workerConfig)
  const guild = useGuildStore()
  guild.currentGuild = { id: 'g1', name: 'Test Guild' }
  const auth = useAuthStore()
  auth.loginToken = 'tok'
  const wrapper = mount(GuildSettingsPanel)
  await flushPromises()
  const tab = wrapper.findAll('.settings-tab').find((t) => t.text() === label)
  await tab!.trigger('click')
  await flushPromises()
  return wrapper
}

const openForemanTab = (cfg: Record<string, unknown>, workerCfg: Record<string, unknown> = {}) =>
  openTab('Foreman', cfg, workerCfg)
const openWorkerTab = (workerCfg: Record<string, unknown>) =>
  openTab('Worker Settings', {}, workerCfg)

describe('GuildSettingsPanel foreman provider/model', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('preserves the bedrock model when toggling the provider off and back', async () => {
    const wrapper = await openForemanTab({ provider: 'bedrock', model: BEDROCK_ARN })

    const select = wrapper.find('select')
    const modelInput = () =>
      wrapper.find('input[list="foreman-model-hints"]').element as HTMLInputElement

    // Loaded config: bedrock provider with its custom ARN model.
    expect((select.element as HTMLSelectElement).value).toBe('bedrock')
    expect(modelInput().value).toBe(BEDROCK_ARN)

    // Switch off bedrock → the bedrock-specific model is swapped out (anthropic
    // would reject the ARN), so the field clears.
    await select.setValue('')
    expect(modelInput().value).toBe('')

    // Switch back to bedrock → the ARN is restored rather than lost.
    await select.setValue('bedrock')
    expect(modelInput().value).toBe(BEDROCK_ARN)
  })
})

describe('GuildSettingsPanel worker tool tabs', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('defaults to the General sub-tab; the foreman LLM fields stay in the Foreman tab', async () => {
    const wrapper = await openWorkerTab({})
    const toolTabs = wrapper.findAll('.foreman-tool-tab')
    expect(toolTabs.map((t) => t.text())).toEqual(['General', 'Claude', 'Pi', 'Codex'])
    expect(toolTabs.find((t) => t.text() === 'General')!.classes()).toContain('active')
    // The foreman's own model input never renders under Worker Settings.
    expect(wrapper.find('input[list="foreman-model-hints"]').exists()).toBe(false)
  })

  it('lets the Pi default model be set and saved', async () => {
    const wrapper = await openWorkerTab({ toolDefaults: { pi: { model: 'claude-sonnet-4-6' } } })

    const piTab = wrapper.findAll('.foreman-tool-tab').find((t) => t.text() === 'Pi')
    await piTab!.trigger('click')

    const inputs = wrapper.findAll('.foreman-field input.settings-input')
    const piModelInput = inputs[0].element as HTMLInputElement
    expect(piModelInput.value).toBe('claude-sonnet-4-6')

    await inputs[0].setValue('claude-opus-4-8')

    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ toolDefaults: { pi: { model: 'claude-opus-4-8' } } }),
    } as Response)
    const saveBtn = wrapper.findAll('button').find((b) => b.text() === 'Save')
    await saveBtn!.trigger('click')
    await flushPromises()

    const putCall = fetchSpy.mock.calls.find(([, init]) => (init as RequestInit)?.method === 'PUT')
    expect(putCall).toBeTruthy()
    const body = JSON.parse((putCall![1] as RequestInit).body as string)
    expect(body.toolDefaults.pi.model).toBe('claude-opus-4-8')
  })

  it('lets the Pi default provider be set to Bedrock and saved', async () => {
    const wrapper = await openWorkerTab({ toolDefaults: { pi: { provider: 'anthropic' } } })

    const piTab = wrapper.findAll('.foreman-tool-tab').find((t) => t.text() === 'Pi')
    await piTab!.trigger('click')

    const piProviderSelect = wrapper.find('select')
    expect((piProviderSelect.element as HTMLSelectElement).value).toBe('anthropic')

    const options = piProviderSelect.findAll('option').map((o) => o.element.value)
    expect(options).toContain('bedrock')

    await piProviderSelect.setValue('bedrock')

    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ toolDefaults: { pi: { provider: 'bedrock' } } }),
    } as Response)
    const saveBtn = wrapper.findAll('button').find((b) => b.text() === 'Save')
    await saveBtn!.trigger('click')
    await flushPromises()

    const putCall = fetchSpy.mock.calls.find(([, init]) => (init as RequestInit)?.method === 'PUT')
    expect(putCall).toBeTruthy()
    const body = JSON.parse((putCall![1] as RequestInit).body as string)
    expect(body.toolDefaults.pi.provider).toBe('bedrock')
  })
})

describe('GuildSettingsPanel worker vs foreman env split', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  const keyValues = (wrapper: ReturnType<typeof mount>) =>
    wrapper.findAll('input.env-var-key').map((i) => (i.element as HTMLInputElement).value)

  it('splits env by destination (no forward checkbox) and saves the right flags', async () => {
    // Worker env lives in spawn-settings; foreman env lives in foreman-config.
    const wrapper = await openForemanTab(
      { env_vars: [{ key: 'FOREMAN_ONLY', value: 'x', forward: false }] },
      { envVars: [{ key: 'ANTHROPIC_API_KEY', value: 'sk' }] },
    )

    // The per-row forward checkbox is gone.
    expect(wrapper.find('input[type="checkbox"].env-var-fwd').exists()).toBe(false)
    // Foreman tab shows only the foreman-only var, as an editable input.
    expect(keyValues(wrapper)).toEqual(['FOREMAN_ONLY'])

    // Worker Settings → General (default sub-tab) shows the forwarded var, editable.
    const workerTab = wrapper.findAll('.settings-tab').find((t) => t.text() === 'Worker Settings')
    await workerTab!.trigger('click')
    await flushPromises()
    expect(keyValues(wrapper)).toEqual(['ANTHROPIC_API_KEY'])

    // Add a new worker-wide var in General.
    const addBtn = wrapper
      .findAll('.env-var-add-btn')
      .find((b) => b.text().includes('Add Variable'))
    await addBtn!.trigger('click')
    const keyInputs = wrapper.findAll('input.env-var-key')
    await keyInputs[keyInputs.length - 1].setValue('WORKER_NEW')
    const valInputs = wrapper.findAll('input.env-var-value')
    await valInputs[valInputs.length - 1].setValue('v')

    // Save (Worker Settings tab) → foreman-only vars and worker vars go to separate APIs.
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ env_vars: [] }))
    const saveBtn = wrapper.findAll('.foreman-actions button').find((b) => b.text() === 'Save')
    await saveBtn!.trigger('click')
    await flushPromises()

    const patchCall = fetchSpy.mock.calls.find(
      ([url, init]) =>
        String(url).includes('/foreman-config') && (init as RequestInit)?.method === 'PATCH',
    )
    const foremanBody = JSON.parse((patchCall![1] as RequestInit).body as string)
    expect(foremanBody.env_vars).toEqual([{ key: 'FOREMAN_ONLY', value: 'x', forward: false }])

    const putCall = fetchSpy.mock.calls.find(
      ([url, init]) =>
        String(url).includes('/spawn-settings') && (init as RequestInit)?.method === 'PUT',
    )
    const workerBody = JSON.parse((putCall![1] as RequestInit).body as string)
    expect(workerBody.envVars).toEqual([
      { key: 'ANTHROPIC_API_KEY', value: 'sk' },
      { key: 'WORKER_NEW', value: 'v' },
    ])
  })
})
