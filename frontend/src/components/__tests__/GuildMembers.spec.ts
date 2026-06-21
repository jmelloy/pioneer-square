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

function mountAs(userId: string | null, members: unknown[], invites: unknown[] = []) {
  const auth = useAuthStore()
  auth.user = userId ? { id: userId, login: 'me' } : null
  auth.loginToken = 'tok'
  const fetchMock = vi.spyOn(globalThis, 'fetch')
  fetchMock.mockResolvedValueOnce(jsonResponse(members)) // initial load members
  fetchMock.mockResolvedValueOnce(jsonResponse(invites)) // initial load invites
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
    fetchMock.mockResolvedValueOnce(jsonResponse({ user_id: 'u-member', role: 'member' })) // POST members
    fetchMock.mockResolvedValueOnce(jsonResponse([OWNER, MEMBER])) // reload members
    fetchMock.mockResolvedValueOnce(jsonResponse([])) // reload invites
    await wrapper.find('.invite-input').setValue('bob')
    await wrapper.find('.invite-btn').trigger('click')
    await flushPromises()
    const postCall = fetchMock.mock.calls[2] // calls[0]=members, calls[1]=invites, calls[2]=POST
    expect(postCall[0]).toContain('/api/guilds/g1/members')
    expect(JSON.parse((postCall[1] as RequestInit).body as string)).toEqual({
      user: 'bob',
      role: 'member',
    })
    expect(wrapper.findAll('.member-name')).toHaveLength(2)
  })

  it('falls back to a pending invite when the user has not logged in (404)', async () => {
    const { wrapper, fetchMock } = mountAs('u-owner', [OWNER])
    await flushPromises()
    // POST /members → 404 with the exact backend "user not found" message format
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        {
          detail:
            "User 'ghost' not found. They must log in to Pioneer Square once before they can be added.",
        },
        404,
      ),
    )
    // POST /invites → success
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ id: 1, github_login: 'ghost', github_id: null, role: 'member', status: 'pending' }),
    )
    // reload members
    fetchMock.mockResolvedValueOnce(jsonResponse([OWNER]))
    // reload invites
    fetchMock.mockResolvedValueOnce(
      jsonResponse([{ id: 1, github_login: 'ghost', github_id: null, role: 'member', status: 'pending' }]),
    )
    await wrapper.find('.invite-input').setValue('ghost')
    await wrapper.find('.invite-btn').trigger('click')
    await flushPromises()
    expect(wrapper.find('.members-hint').text()).toContain("they'll be added when they first log in")
    expect(wrapper.find('.members-error').exists()).toBe(false)
  })

  it('shows an error when both direct add and pending invite fail', async () => {
    const { wrapper, fetchMock } = mountAs('u-owner', [OWNER])
    await flushPromises()
    // POST /members → 404 with user-not-found message, triggering the invite fallback
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        {
          detail:
            "User 'ghost' not found. They must log in to Pioneer Square once before they can be added.",
        },
        404,
      ),
    )
    // POST /invites → also fails
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: 'Guild not found.' }, 404))
    await wrapper.find('.invite-input').setValue('ghost')
    await wrapper.find('.invite-btn').trigger('click')
    await flushPromises()
    expect(wrapper.find('.members-error').exists()).toBe(true)
  })

  it('surfaces a non-user-not-found 404 as an error without attempting an invite', async () => {
    const { wrapper, fetchMock } = mountAs('u-owner', [OWNER])
    await flushPromises()
    // POST /members → 404 with a guild-level error (should NOT fall back to invite)
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: 'Guild not found.' }, 404))
    await wrapper.find('.invite-input').setValue('ghost')
    await wrapper.find('.invite-btn').trigger('click')
    await flushPromises()
    expect(wrapper.find('.members-error').text()).toContain('Guild not found.')
    // Only 3 fetch calls total: load members, load invites, POST members (no POST invites)
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })

  it('does not allow removing the last owner', async () => {
    const { wrapper } = mountAs('u-owner', [OWNER])
    await flushPromises()
    // Only owner present → no remove button / role select for them.
    expect(wrapper.find('.member-remove-btn').exists()).toBe(false)
    expect(wrapper.find('.member-role-badge--owner').exists()).toBe(true)
  })
})
