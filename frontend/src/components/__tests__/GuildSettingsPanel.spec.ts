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

async function openForemanTab(foremanConfig: Record<string, unknown>) {
  mockFetch(foremanConfig)
  const guild = useGuildStore()
  guild.currentGuild = { id: 'g1', name: 'Test Guild' }
  const auth = useAuthStore()
  auth.loginToken = 'tok'
  const wrapper = mount(GuildSettingsPanel)
  await flushPromises()
  const foremanTab = wrapper.findAll('.settings-tab').find((t) => t.text() === 'Foreman')
  await foremanTab!.trigger('click')
  await flushPromises()
  return wrapper
}

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

describe('GuildSettingsPanel foreman tool tabs', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('defaults to the Claude sub-tab and shows the existing provider/model fields', async () => {
    const wrapper = await openForemanTab({})
    const toolTabs = wrapper.findAll('.foreman-tool-tab')
    expect(toolTabs.map((t) => t.text())).toEqual(['Claude', 'Pi', 'Codex'])
    expect(toolTabs.find((t) => t.text() === 'Claude')!.classes()).toContain('active')
    expect(wrapper.find('select').exists()).toBe(true)
    expect(wrapper.find('input[list="foreman-model-hints"]').exists()).toBe(true)
  })

  it('lets the Pi default model be set and saved', async () => {
    const wrapper = await openForemanTab({ pi_default_model: 'claude-sonnet-4-6' })

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
})
