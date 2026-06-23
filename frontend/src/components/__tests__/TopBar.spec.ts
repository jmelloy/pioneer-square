import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import TopBar from '../TopBar.vue'
import { useGuildStore } from '../../stores/guild'
import { useAuthStore } from '../../stores/auth'

const BEDROCK_ARN =
  'arn:aws:bedrock:us-east-1:446872464738:inference-profile/us.anthropic.claude-sonnet-4-6'

const router = createRouter({
  history: createMemoryHistory(),
  routes: [{ path: '/', component: { template: '<div />' } }],
})

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
    if (url.includes('/auth/claude-credentials')) {
      return Promise.resolve(jsonResponse({ saved: false }))
    }
    return Promise.resolve(jsonResponse({}))
  })
}

async function openForemanConfig(foremanConfig: Record<string, unknown>) {
  mockFetch(foremanConfig)
  const guild = useGuildStore()
  guild.currentGuild = { id: 'g1', name: 'Test Guild' }
  const auth = useAuthStore()
  auth.loginToken = 'tok'
  const wrapper = mount(TopBar, { global: { plugins: [router] } })
  await flushPromises()
  await wrapper.find('.settings-btn').trigger('click') // open settings popover
  await flushPromises()
  await wrapper.find('.foreman-toggle-btn').trigger('click') // open foreman config
  await flushPromises()
  return wrapper
}

describe('TopBar foreman provider/model', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('preserves the bedrock model when toggling the provider off and back', async () => {
    const wrapper = await openForemanConfig({ provider: 'bedrock', model: BEDROCK_ARN })

    const select = wrapper.find('.foreman-config-section select')
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
