import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import GuildMembers from '../GuildMembers.vue'
import { useAuthStore } from '../../stores/auth'

const OWNER = {
  user_id: 'u-owner',
  role: 'owner',
  created_at: '2026-01-01T00:00:00Z',
  github_login: 'owner',
  display_name: 'Owner',
  avatar_url: '',
}
const MEMBER = {
  user_id: 'u-member',
  role: 'member',
  created_at: '2026-01-02T00:00:00Z',
  github_login: 'bob',
  display_name: 'Bob',
  avatar_url: '',
}

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response
}

function mountAs(userId: string | null, members: unknown[]) {
  const auth = useAuthStore()
  auth.user = userId ? { id: userId, login: 'me' } : null
  auth.loginToken = 'tok'
  const fetchMock = vi.spyOn(globalThis, 'fetch')
  fetchMock.mockResolvedValueOnce(jsonResponse(members)) // initial load
  const wrapper = mount(GuildMembers, { props: { guildId: 'g1' } })
  return { wrapper, fetchMock }
}

describe('GuildMembers', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('lists members fetched from the API', async () => {
    const { wrapper } = mountAs('u-owner', [OWNER, MEMBER])
    await flushPromises()
    const names = wrapper.findAll('.member-name').map((n) => n.text())
    expect(names).toContain('owner')
    expect(names).toContain('bob')
  })

  it('shows the invite form to owners', async () => {
    const { wrapper } = mountAs('u-owner', [OWNER, MEMBER])
    await flushPromises()
    expect(wrapper.find('.invite-form').exists()).toBe(true)
  })

  it('hides invite controls for non-owners', async () => {
    const { wrapper } = mountAs('u-member', [OWNER, MEMBER])
    await flushPromises()
    expect(wrapper.find('.invite-form').exists()).toBe(false)
    expect(wrapper.text()).toContain('Only owners can invite members')
  })

  it('posts an invite and reloads the list', async () => {
    const { wrapper, fetchMock } = mountAs('u-owner', [OWNER])
    await flushPromises()
    fetchMock.mockResolvedValueOnce(jsonResponse({ user_id: 'u-member', role: 'member' })) // POST
    fetchMock.mockResolvedValueOnce(jsonResponse([OWNER, MEMBER])) // reload
    await wrapper.find('.invite-input').setValue('bob')
    await wrapper.find('.invite-btn').trigger('click')
    await flushPromises()
    const postCall = fetchMock.mock.calls[1]
    expect(postCall[0]).toContain('/api/guilds/g1/members')
    expect(JSON.parse((postCall[1] as RequestInit).body as string)).toEqual({
      user: 'bob',
      role: 'member',
    })
    expect(wrapper.findAll('.member-name')).toHaveLength(2)
  })

  it('surfaces the API error when inviting an unknown user', async () => {
    const { wrapper, fetchMock } = mountAs('u-owner', [OWNER])
    await flushPromises()
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: 'User not found.' }, 404))
    await wrapper.find('.invite-input').setValue('ghost')
    await wrapper.find('.invite-btn').trigger('click')
    await flushPromises()
    expect(wrapper.find('.members-error').text()).toContain('User not found.')
  })

  it('does not allow removing the last owner', async () => {
    const { wrapper } = mountAs('u-owner', [OWNER])
    await flushPromises()
    // Only owner present → no remove button / role select for them.
    expect(wrapper.find('.member-remove-btn').exists()).toBe(false)
    expect(wrapper.find('.member-role-badge--owner').exists()).toBe(true)
  })
})
