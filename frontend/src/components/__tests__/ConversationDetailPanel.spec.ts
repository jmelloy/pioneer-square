import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import ConversationDetailPanel from '../ConversationDetailPanel.vue'
import { useGuildStore } from '../../stores/guild'
import { useConversationsStore } from '../../stores/conversations'
import { api, ApiError } from '../../utils/api'

vi.mock('../../utils/api', async () => {
  const actual = await vi.importActual<typeof import('../../utils/api')>('../../utils/api')
  return { ...actual, api: vi.fn() }
})

const apiMock = vi.mocked(api)

const CONVERSATION = {
  id: 42,
  user_id: 'u-1',
  discord_thread_id: null,
  name: 'Deploy pipeline',
  status: 'active' as const,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

function mountPanel() {
  const guildStore = useGuildStore()
  guildStore.currentGuild = { id: 'g1', name: 'Guild 1' }
  const conversationsStore = useConversationsStore()
  conversationsStore.conversations = [{ ...CONVERSATION }]

  apiMock.mockResolvedValueOnce([]) // fetchConversationMessages on mount

  const wrapper = mount(ConversationDetailPanel, { props: { id: 42 } })
  return { wrapper, guildStore, conversationsStore }
}

describe('ConversationDetailPanel reply', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('disables send while input is empty', async () => {
    const { wrapper } = mountPanel()
    await flushPromises()
    const button = wrapper.find('.send-btn')
    expect((button.element as HTMLButtonElement).disabled).toBe(true)
  })

  it('posts the reply and appends it to the thread on success', async () => {
    const { wrapper } = mountPanel()
    await flushPromises()

    apiMock.mockResolvedValueOnce({
      id: 7,
      conversationId: 42,
      content: 'sounds good',
      createdAt: '2026-01-02T00:00:00Z',
    })

    await wrapper.find('.reply-input').setValue('sounds good')
    await wrapper.find('.send-btn').trigger('click')
    await flushPromises()

    expect(apiMock).toHaveBeenLastCalledWith(
      '/api/guilds/g1/conversations/42/messages',
      expect.objectContaining({ method: 'POST', json: { content: 'sounds good' } }),
    )
    expect(wrapper.text()).toContain('sounds good')
    expect((wrapper.find('.reply-input').element as HTMLTextAreaElement).value).toBe('')
  })

  it('shows an error message when posting fails', async () => {
    const { wrapper } = mountPanel()
    await flushPromises()

    apiMock.mockRejectedValueOnce(new ApiError('Too many messages — please slow down', 429))

    await wrapper.find('.reply-input').setValue('spam')
    await wrapper.find('.send-btn').trigger('click')
    await flushPromises()

    expect(wrapper.find('.reply-error').text()).toBe('Too many messages — please slow down')
  })

  it('does not show the reply input for closed conversations', async () => {
    const guildStore = useGuildStore()
    guildStore.currentGuild = { id: 'g1', name: 'Guild 1' }
    const conversationsStore = useConversationsStore()
    conversationsStore.conversations = [{ ...CONVERSATION, status: 'closed' }]
    apiMock.mockResolvedValueOnce([])

    const wrapper = mount(ConversationDetailPanel, { props: { id: 42 } })
    await flushPromises()

    expect(wrapper.find('.reply-row').exists()).toBe(false)
  })
})
