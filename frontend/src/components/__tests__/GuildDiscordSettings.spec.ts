import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import GuildDiscordSettings from '../GuildDiscordSettings.vue'
import { useAuthStore } from '../../stores/auth'

const STATUS = {
  bot_configured: true,
  bot_token_set: true,
  channel_id_set: true,
  slash_commands_configured: false,
  stream_tasks_enabled: true,
  pioneer_guild_slug: 'my-guild',
  allowed_role_ids: ['111', '222'],
  operator_role_name: 'Pioneer Square Operator',
  gateway: { enabled: true, running: true },
}

const ACCOUNT = {
  user_id: 'u-owner',
  github_login: 'owner',
  display_name: 'Owner',
  avatar_url: '',
  discord_user_id: 'dc-1',
  discord_username: 'ownerDisc',
  linked_at: '2026-01-01T00:00:00Z',
}

const BOT = {
  user_id: 'discordbot:111',
  display_name: 'SomeBot',
  discord_channel_id: 'chan-1',
  parent_user_id: 'u-owner',
  created_at: '2026-01-01T00:00:00Z',
  role: 'member',
}

const CHANNEL = {
  id: 1,
  discord_guild_id: 'dg-1',
  discord_channel_id: 'dc-chan-1',
  ps_guild_id: 'g1',
  created_at: '2026-01-01T00:00:00Z',
}

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response
}

function mockFetch(overrides: Record<string, unknown> = {}) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/discord/status'))
      return Promise.resolve(jsonResponse(overrides.status ?? STATUS))
    if (url.includes('/discord/accounts'))
      return Promise.resolve(jsonResponse(overrides.accounts ?? [ACCOUNT]))
    if (url.includes('/discord/bots')) return Promise.resolve(jsonResponse(overrides.bots ?? [BOT]))
    if (url.includes('/discord/channels'))
      return Promise.resolve(jsonResponse(overrides.channels ?? [CHANNEL]))
    if (url.includes('/members')) return Promise.resolve(jsonResponse(overrides.members ?? []))
    return Promise.resolve(jsonResponse({}))
  })
}

function mountAs(userId: string | null, overrides: Record<string, unknown> = {}) {
  const auth = useAuthStore()
  auth.user = userId ? { id: userId, login: 'me' } : null
  auth.loginToken = 'tok'
  mockFetch(overrides)
  const wrapper = mount(GuildDiscordSettings, { props: { guildId: 'g1' } })
  return wrapper
}

async function openSubTab(wrapper: ReturnType<typeof mount>, label: string) {
  const tab = wrapper.findAll('.foreman-tool-tab').find((t) => t.text() === label)
  await tab!.trigger('click')
  await flushPromises()
}

describe('GuildDiscordSettings', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows Discord status on the default tab', async () => {
    const wrapper = mountAs('u-owner', { members: [{ user_id: 'u-owner', role: 'owner' }] })
    await flushPromises()
    expect(wrapper.text()).toContain('Running')
    expect(wrapper.text()).toContain('Pioneer Square Operator')
    expect(wrapper.text()).toContain('my-guild')
  })

  it('lists linked Discord accounts', async () => {
    const wrapper = mountAs('u-owner', { members: [{ user_id: 'u-owner', role: 'owner' }] })
    await flushPromises()
    await openSubTab(wrapper, 'Accounts')
    expect(wrapper.text()).toContain('owner')
    expect(wrapper.text()).toContain('ownerDisc')
  })

  it('lets an owner unlink a Discord account', async () => {
    const fetchMock = mockFetch()
    const auth = useAuthStore()
    auth.user = { id: 'u-owner', login: 'me' }
    auth.loginToken = 'tok'
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/members'))
        return Promise.resolve(jsonResponse([{ user_id: 'u-owner', role: 'owner' }]))
      if (url.includes('/discord/status')) return Promise.resolve(jsonResponse(STATUS))
      if (url.includes('/discord/accounts')) return Promise.resolve(jsonResponse([ACCOUNT]))
      if (url.includes('/discord/bots')) return Promise.resolve(jsonResponse([BOT]))
      if (url.includes('/discord/channels')) return Promise.resolve(jsonResponse([CHANNEL]))
      return Promise.resolve(jsonResponse({}))
    })
    const wrapper = mount(GuildDiscordSettings, { props: { guildId: 'g1' } })
    await flushPromises()
    await openSubTab(wrapper, 'Accounts')

    fetchMock.mockResolvedValueOnce(jsonResponse({ status: 'unlinked' }))
    fetchMock.mockResolvedValueOnce(jsonResponse([]))
    await wrapper.find('.discord-remove-btn').trigger('click')
    await flushPromises()

    const deleteCall = fetchMock.mock.calls.find(
      ([, init]) => (init as RequestInit | undefined)?.method === 'DELETE',
    )
    expect(deleteCall?.[0]).toContain('/discord/accounts/dc-1')
    expect(wrapper.text()).toContain('No Discord accounts linked yet.')
  })

  it('hides owner-only controls for non-owners', async () => {
    const wrapper = mountAs('u-member', { members: [{ user_id: 'u-member', role: 'member' }] })
    await flushPromises()
    await openSubTab(wrapper, 'Accounts')
    expect(wrapper.find('.discord-remove-btn').exists()).toBe(false)
  })

  it('lists auto-provisioned bots read-only', async () => {
    const wrapper = mountAs('u-owner', { members: [{ user_id: 'u-owner', role: 'owner' }] })
    await flushPromises()
    await openSubTab(wrapper, 'Bots')
    expect(wrapper.text()).toContain('SomeBot')
    expect(wrapper.find('.discord-remove-btn').exists()).toBe(false)
  })

  it('lists channel bindings and lets an owner add one', async () => {
    const fetchMock = mockFetch({ channels: [CHANNEL] })
    const auth = useAuthStore()
    auth.user = { id: 'u-owner', login: 'me' }
    auth.loginToken = 'tok'
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/members'))
        return Promise.resolve(jsonResponse([{ user_id: 'u-owner', role: 'owner' }]))
      if (url.includes('/discord/status')) return Promise.resolve(jsonResponse(STATUS))
      if (url.includes('/discord/accounts')) return Promise.resolve(jsonResponse([ACCOUNT]))
      if (url.includes('/discord/bots')) return Promise.resolve(jsonResponse([BOT]))
      if (url.includes('/discord/channels')) return Promise.resolve(jsonResponse([CHANNEL]))
      return Promise.resolve(jsonResponse({}))
    })
    const wrapper = mount(GuildDiscordSettings, { props: { guildId: 'g1' } })
    await flushPromises()
    await openSubTab(wrapper, 'Channels')
    expect(wrapper.text()).toContain('dg-1')
    expect(wrapper.text()).toContain('dc-chan-1')

    const inputs = wrapper.findAll('.discord-add-row input')
    await inputs[0].setValue('dg-new')
    await inputs[1].setValue('dc-new')

    fetchMock.mockResolvedValueOnce(
      jsonResponse({ id: 2, discord_guild_id: 'dg-new', discord_channel_id: 'dc-new' }),
    )
    fetchMock.mockResolvedValueOnce(jsonResponse([CHANNEL, { ...CHANNEL, id: 2 }]))
    await wrapper.find('.discord-add-form button').trigger('click')
    await flushPromises()

    const postCall = fetchMock.mock.calls.find(
      ([, init]) => (init as RequestInit | undefined)?.method === 'POST',
    )
    expect(postCall?.[0]).toContain('/discord/channels')
    expect(JSON.parse((postCall![1] as RequestInit).body as string)).toEqual({
      discord_guild_id: 'dg-new',
      discord_channel_id: 'dc-new',
    })
  })
})
