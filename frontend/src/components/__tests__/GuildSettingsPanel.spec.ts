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
