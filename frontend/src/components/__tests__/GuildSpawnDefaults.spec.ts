import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import GuildSpawnDefaults from '../GuildSpawnDefaults.vue'
import { useAuthStore } from '../../stores/auth'

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response
}

interface Routes {
  members?: unknown
  defaults?: unknown
}

/** Dispatch fetch by URL so concurrent onMounted calls resolve deterministically. */
function mockFetch(routes: Routes, onPut?: (body: unknown) => unknown) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
    const url = String(input)
    if (url.includes('/members')) return Promise.resolve(jsonResponse(routes.members ?? []))
    if (url.includes('/spawn-defaults')) {
      if (init?.method === 'PUT') {
        const body = init.body ? JSON.parse(init.body as string) : {}
        return Promise.resolve(jsonResponse(onPut ? onPut(body) : body))
      }
      return Promise.resolve(jsonResponse(routes.defaults ?? {}))
    }
    return Promise.resolve(jsonResponse({}))
  })
}

function mountAs(userId: string) {
  const auth = useAuthStore()
  auth.user = { id: userId, login: 'me' }
  auth.loginToken = 'tok'
  return mount(GuildSpawnDefaults, { props: { guildId: 'g1' } })
}

describe('GuildSpawnDefaults', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows an editable form with a Save button to owners', async () => {
    mockFetch({
      members: [{ user_id: 'u-owner', role: 'owner' }],
      defaults: { repos: [], tools: [], agent_count: null },
    })
    const wrapper = mountAs('u-owner')
    await flushPromises()
    expect(wrapper.find('.gsd-save-btn').exists()).toBe(true)
    expect(wrapper.find('.gsd-readonly').exists()).toBe(false)
  })

  it('shows a read-only view to non-owners', async () => {
    mockFetch({
      members: [{ user_id: 'u-member', role: 'member' }],
      defaults: { repos: ['a/b'], tools: ['claude'], agent_count: 3 },
    })
    const wrapper = mountAs('u-member')
    await flushPromises()
    expect(wrapper.find('.gsd-save-btn').exists()).toBe(false)
    expect(wrapper.find('.gsd-readonly').exists()).toBe(true)
    expect(wrapper.text()).toContain('Only owners can change')
    expect(wrapper.text()).toContain('a/b')
  })

  it('saves the edited defaults via PUT', async () => {
    const put = vi.fn((body: unknown) => body)
    mockFetch(
      {
        members: [{ user_id: 'u-owner', role: 'owner' }],
        defaults: { repos: [], tools: ['claude'], agent_count: 2 },
      },
      put,
    )
    const wrapper = mountAs('u-owner')
    await flushPromises()
    await wrapper.find('.gsd-save-btn').trigger('click')
    await flushPromises()
    expect(put).toHaveBeenCalledOnce()
    expect(put).toHaveBeenCalledWith({ repos: [], tools: ['claude'], agent_count: 2 })
    expect(wrapper.find('.save-status-saved').exists()).toBe(true)
  })
})
