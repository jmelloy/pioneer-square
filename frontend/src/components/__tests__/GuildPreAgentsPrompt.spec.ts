import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import GuildPreAgentsPrompt from '../GuildPreAgentsPrompt.vue'
import { useAuthStore } from '../../stores/auth'

function jsonResponse(body: unknown, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response
}

function mockFetch(role: string, settings: unknown) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
    const url = String(input)
    if (url.includes('/members')) return Promise.resolve(jsonResponse([{ user_id: 'u1', role }]))
    if (url.includes('/settings')) {
      if (init?.method === 'PATCH') {
        const body = JSON.parse(init.body as string)
        return Promise.resolve(
          jsonResponse({
            pre_agents_prompt: body.pre_agents_prompt,
            pre_agents_prompt_updated_at: body.pre_agents_prompt ? '2026-11-11T00:00:00Z' : null,
          }),
        )
      }
      return Promise.resolve(jsonResponse(settings))
    }
    return Promise.resolve(jsonResponse({}))
  })
}

function mountIt() {
  const auth = useAuthStore()
  auth.user = { id: 'u1', login: 'me' }
  auth.loginToken = 'tok'
  return mount(GuildPreAgentsPrompt, { props: { guildId: 'g1' } })
}

describe('GuildPreAgentsPrompt', () => {
  beforeEach(() => setActivePinia(createPinia()))
  afterEach(() => vi.restoreAllMocks())

  it('renders nothing for non-owners', async () => {
    mockFetch('member', { pre_agents_prompt: 'secret', pre_agents_prompt_updated_at: null })
    const wrapper = mountIt()
    await flushPromises()
    expect(wrapper.find('.gpap').exists()).toBe(false)
  })

  it('shows current value, last updated and the precedence docs to owners', async () => {
    mockFetch('owner', {
      pre_agents_prompt: 'Be careful',
      pre_agents_prompt_updated_at: '2026-11-10T00:00:00Z',
    })
    const wrapper = mountIt()
    await flushPromises()
    expect((wrapper.find('textarea').element as HTMLTextAreaElement).value).toBe('Be careful')
    expect(wrapper.find('.gpap-updated').text()).toContain('Last updated')
    expect(wrapper.text()).toContain('PIONEER_PRE_AGENTS_PROMPT')
  })

  it('saves edits via PATCH', async () => {
    const spy = mockFetch('owner', { pre_agents_prompt: null, pre_agents_prompt_updated_at: null })
    const wrapper = mountIt()
    await flushPromises()
    await wrapper.find('textarea').setValue('New prompt')
    await wrapper.find('.gpap-save-btn').trigger('click')
    await flushPromises()
    const call = spy.mock.calls.find(([, init]) => init?.method === 'PATCH')!
    expect(JSON.parse(call[1]!.body as string)).toEqual({ pre_agents_prompt: 'New prompt' })
    expect(wrapper.find('.save-status-saved').exists()).toBe(true)
  })

  it('clear sends null and empties the textarea', async () => {
    const spy = mockFetch('owner', {
      pre_agents_prompt: 'old',
      pre_agents_prompt_updated_at: '2026-11-10T00:00:00Z',
    })
    const wrapper = mountIt()
    await flushPromises()
    await wrapper.find('.gpap-clear-btn').trigger('click')
    await flushPromises()
    const call = spy.mock.calls.find(([, init]) => init?.method === 'PATCH')!
    expect(JSON.parse(call[1]!.body as string)).toEqual({ pre_agents_prompt: null })
    expect((wrapper.find('textarea').element as HTMLTextAreaElement).value).toBe('')
  })
})
