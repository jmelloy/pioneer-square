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
function mockFetch(foremanConfig: Record<string, unknown>) {
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
    return Promise.resolve(jsonResponse({}))
  })
}

async function openTab(label: string, foremanConfig: Record<string, unknown>) {
  mockFetch(foremanConfig)
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

const openForemanTab = (cfg: Record<string, unknown>) => openTab('Foreman', cfg)
const openWorkerTab = (cfg: Record<string, unknown>) => openTab('Worker Settings', cfg)

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
    const wrapper = await openWorkerTab({ pi_default_model: 'claude-sonnet-4-6' })

    const piTab = wrapper.findAll('.foreman-tool-tab').find((t) => t.text() === 'Pi')
    await piTab!.trigger('click')

    const inputs = wrapper.findAll('.foreman-field input.settings-input')
    const piModelInput = inputs[0].element as HTMLInputElement
    expect(piModelInput.value).toBe('claude-sonnet-4-6')

    await inputs[0].setValue('claude-opus-4-8')

    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      {
        ok: true,
        status: 200,
        json: async () => ({ pi_default_model: 'claude-opus-4-8' }),
      } as Response,
    )
    const saveBtn = wrapper.findAll('button').find((b) => b.text() === 'Save')
    await saveBtn!.trigger('click')
    await flushPromises()

    const patchCall = fetchSpy.mock.calls.find(([, init]) => (init as RequestInit)?.method === 'PATCH')
    expect(patchCall).toBeTruthy()
    const body = JSON.parse((patchCall![1] as RequestInit).body as string)
    expect(body.pi_default_model).toBe('claude-opus-4-8')
  })

  it('lets the Pi default provider be set to Bedrock and saved', async () => {
    const wrapper = await openWorkerTab({ pi_default_provider: 'anthropic' })

    const piTab = wrapper.findAll('.foreman-tool-tab').find((t) => t.text() === 'Pi')
    await piTab!.trigger('click')

    const piProviderSelect = wrapper.find('select')
    expect((piProviderSelect.element as HTMLSelectElement).value).toBe('anthropic')

    const options = piProviderSelect.findAll('option').map((o) => o.element.value)
    expect(options).toContain('bedrock')

    await piProviderSelect.setValue('bedrock')

    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      {
        ok: true,
        status: 200,
        json: async () => ({ pi_default_provider: 'bedrock' }),
      } as Response,
    )
    const saveBtn = wrapper.findAll('button').find((b) => b.text() === 'Save')
    await saveBtn!.trigger('click')
    await flushPromises()

    const patchCall = fetchSpy.mock.calls.find(([, init]) => (init as RequestInit)?.method === 'PATCH')
    expect(patchCall).toBeTruthy()
    const body = JSON.parse((patchCall![1] as RequestInit).body as string)
    expect(body.pi_default_provider).toBe('bedrock')
  })
})

describe('GuildSettingsPanel foreman env forwarding', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('round-trips the per-var forward flag and reflects it on the Worker General tab', async () => {
    // A stored var with forward=true, another without.
    const wrapper = await openForemanTab({
      env_vars: [
        { key: 'ANTHROPIC_API_KEY', value: 'sk', forward: true },
        { key: 'FOREMAN_ONLY', value: 'x' },
      ],
    })

    const boxes = wrapper.findAll('input[type="checkbox"].env-var-fwd')
    expect(boxes.length).toBe(2)
    expect((boxes[0].element as HTMLInputElement).checked).toBe(true)
    expect((boxes[1].element as HTMLInputElement).checked).toBe(false)

    // Forward the second var too, then save.
    await boxes[1].setValue(true)
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        env_vars: [
          { key: 'ANTHROPIC_API_KEY', value: 'sk', forward: true },
          { key: 'FOREMAN_ONLY', value: 'x', forward: true },
        ],
      }),
    } as Response)
    const saveBtn = wrapper.findAll('.foreman-actions button').find((b) => b.text() === 'Save')
    await saveBtn!.trigger('click')
    await flushPromises()

    const patchCall = fetchSpy.mock.calls.find(([, init]) => (init as RequestInit)?.method === 'PATCH')
    const body = JSON.parse((patchCall![1] as RequestInit).body as string)
    const byKey = Object.fromEntries(body.env_vars.map((e: { key: string; forward: boolean }) => [e.key, e.forward]))
    expect(byKey).toEqual({ ANTHROPIC_API_KEY: true, FOREMAN_ONLY: true })

    // The Worker Settings → General tab lists only forwarded vars.
    const workerTab = wrapper.findAll('.settings-tab').find((t) => t.text() === 'Worker Settings')
    await workerTab!.trigger('click')
    await flushPromises()
    const roKeys = wrapper.findAll('.env-var-ro.env-var-key').map((s) => s.text())
    expect(roKeys).toContain('ANTHROPIC_API_KEY')
    expect(roKeys).toContain('FOREMAN_ONLY')
  })
})
