import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import GuildSpawnProfiles from '../GuildSpawnProfiles.vue'
import { useAuthStore } from '../../stores/auth'

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response
}

const BUILTIN = {
  id: 'claude-default',
  name: 'claude-default',
  description: 'Claude Code via the Anthropic API',
  tool: 'claude',
  provider: 'api-key',
  credentials_source: 'anthropic_api_key',
  default_agent_count: null,
  default_repos: [],
  scope: 'guild',
  builtin: true,
}

interface Routes {
  members?: unknown
  profiles?: unknown
}

/** Dispatch fetch by URL so concurrent onMounted calls resolve deterministically. */
function mockFetch(
  routes: Routes,
  handlers?: {
    onPost?: (body: unknown) => unknown
    onPatch?: (id: string, body: unknown) => unknown
    onDelete?: (id: string) => unknown
  },
) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
    const url = String(input)
    if (url.includes('/members')) return Promise.resolve(jsonResponse(routes.members ?? []))
    if (url.includes('/spawn-profiles')) {
      const method = init?.method ?? 'GET'
      if (method === 'POST') {
        const body = init?.body ? JSON.parse(init.body as string) : {}
        return Promise.resolve(jsonResponse(handlers?.onPost ? handlers.onPost(body) : body))
      }
      if (method === 'PATCH') {
        const id = decodeURIComponent(url.split('/spawn-profiles/')[1])
        const body = init?.body ? JSON.parse(init.body as string) : {}
        return Promise.resolve(
          jsonResponse(handlers?.onPatch ? handlers.onPatch(id, body) : { id, ...body }),
        )
      }
      if (method === 'DELETE') {
        const id = decodeURIComponent(url.split('/spawn-profiles/')[1])
        return Promise.resolve(jsonResponse(handlers?.onDelete ? handlers.onDelete(id) : {}))
      }
      return Promise.resolve(jsonResponse(routes.profiles ?? []))
    }
    return Promise.resolve(jsonResponse({}))
  })
}

function mountAs(userId: string) {
  const auth = useAuthStore()
  auth.user = { id: userId, login: 'me' }
  auth.loginToken = 'tok'
  return mount(GuildSpawnProfiles, { props: { guildId: 'g1' } })
}

describe('GuildSpawnProfiles', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('lists profiles including built-ins', async () => {
    mockFetch({
      members: [{ user_id: 'u-owner', role: 'owner' }],
      profiles: [BUILTIN],
    })
    const wrapper = mountAs('u-owner')
    await flushPromises()
    expect(wrapper.text()).toContain('claude-default')
    expect(wrapper.text()).toContain('built-in')
    // built-in rows have no edit/delete controls
    expect(wrapper.find('.gsp-icon-btn').exists()).toBe(false)
  })

  it('lets an owner create a guild-scoped profile', async () => {
    const post = vi.fn((body: unknown) => ({
      id: 'sp-abc123',
      description: null,
      default_agent_count: null,
      default_repos: [],
      builtin: false,
      ...(body as Record<string, unknown>),
    }))
    mockFetch({ members: [{ user_id: 'u-owner', role: 'owner' }], profiles: [] }, { onPost: post })
    const wrapper = mountAs('u-owner')
    await flushPromises()

    await wrapper.find('.gsp-add-btn').trigger('click')
    await wrapper.find('input.gsp-input').setValue('claude-aws')
    await wrapper.find('.gsp-save-btn').trigger('click')
    await flushPromises()

    expect(post).toHaveBeenCalledOnce()
    expect(post).toHaveBeenCalledWith(
      expect.objectContaining({ name: 'claude-aws', tool: 'claude', scope: 'guild' }),
    )
    expect(wrapper.text()).toContain('claude-aws')
    expect(wrapper.find('.gsp-form').exists()).toBe(false)
  })

  it('rejects an empty name without calling the API', async () => {
    const post = vi.fn()
    mockFetch({ members: [{ user_id: 'u-owner', role: 'owner' }], profiles: [] }, { onPost: post })
    const wrapper = mountAs('u-owner')
    await flushPromises()

    await wrapper.find('.gsp-add-btn').trigger('click')
    await wrapper.find('.gsp-save-btn').trigger('click')
    await flushPromises()

    expect(post).not.toHaveBeenCalled()
    expect(wrapper.find('.gsp-error').text()).toContain('Name is required')
  })

  it('edits an existing custom profile via PATCH', async () => {
    const custom = {
      id: 'sp-abc123',
      name: 'my-profile',
      description: null,
      tool: 'claude',
      provider: null,
      credentials_source: null,
      default_agent_count: 2,
      default_repos: ['a/b'],
      scope: 'guild',
      builtin: false,
    }
    const patch = vi.fn((id: string, body: unknown) => ({ ...custom, id, ...(body as object) }))
    mockFetch(
      { members: [{ user_id: 'u-owner', role: 'owner' }], profiles: [custom] },
      { onPatch: patch },
    )
    const wrapper = mountAs('u-owner')
    await flushPromises()

    await wrapper.find('.gsp-icon-btn').trigger('click')
    const nameInput = wrapper.find('input.gsp-input')
    await nameInput.setValue('my-profile-renamed')
    await wrapper.find('.gsp-save-btn').trigger('click')
    await flushPromises()

    expect(patch).toHaveBeenCalledOnce()
    expect(patch).toHaveBeenCalledWith(
      'sp-abc123',
      expect.objectContaining({ name: 'my-profile-renamed' }),
    )
    expect(wrapper.text()).toContain('my-profile-renamed')
  })

  it('deletes a custom profile after confirming', async () => {
    const custom = {
      id: 'sp-abc123',
      name: 'my-profile',
      description: null,
      tool: 'claude',
      provider: null,
      credentials_source: null,
      default_agent_count: null,
      default_repos: [],
      scope: 'guild',
      builtin: false,
    }
    const del = vi.fn(() => ({ status: 'deleted' }))
    mockFetch(
      { members: [{ user_id: 'u-owner', role: 'owner' }], profiles: [custom] },
      { onDelete: del },
    )
    window.confirm = vi.fn(() => true)
    const wrapper = mountAs('u-owner')
    await flushPromises()

    await wrapper.find('.gsp-icon-btn--danger').trigger('click')
    await flushPromises()

    expect(del).toHaveBeenCalledWith('sp-abc123')
    expect(wrapper.text()).not.toContain('my-profile')
  })

  it('does not delete when the confirmation is declined', async () => {
    const custom = {
      id: 'sp-abc123',
      name: 'my-profile',
      description: null,
      tool: 'claude',
      provider: null,
      credentials_source: null,
      default_agent_count: null,
      default_repos: [],
      scope: 'guild',
      builtin: false,
    }
    const del = vi.fn(() => ({ status: 'deleted' }))
    mockFetch(
      { members: [{ user_id: 'u-owner', role: 'owner' }], profiles: [custom] },
      { onDelete: del },
    )
    window.confirm = vi.fn(() => false)
    const wrapper = mountAs('u-owner')
    await flushPromises()

    await wrapper.find('.gsp-icon-btn--danger').trigger('click')
    await flushPromises()

    expect(del).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('my-profile')
  })

  it('hides edit/delete controls for guild-scoped profiles when not an owner', async () => {
    const custom = {
      id: 'sp-abc123',
      name: 'my-profile',
      description: null,
      tool: 'claude',
      provider: null,
      credentials_source: null,
      default_agent_count: null,
      default_repos: [],
      scope: 'guild',
      builtin: false,
    }
    mockFetch({ members: [{ user_id: 'u-member', role: 'member' }], profiles: [custom] })
    const wrapper = mountAs('u-member')
    await flushPromises()

    expect(wrapper.text()).toContain('my-profile')
    expect(wrapper.find('.gsp-icon-btn').exists()).toBe(false)
  })
})
