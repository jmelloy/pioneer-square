import { beforeEach, describe, expect, it } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { mount } from '@vue/test-utils'
import ConversationListRow from '../sidebar/ConversationListRow.vue'
import type { Conversation } from '../../types'

const CONVERSATION: Conversation = {
  id: 1,
  user_id: null,
  discord_thread_id: null,
  name: 'Fix CI pipeline',
  status: 'active',
  created_at: '2025-07-01T00:00:00Z',
  updated_at: '2025-07-01T00:00:00Z',
}

describe('ConversationListRow', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('renders conversation name', () => {
    const wrapper = mount(ConversationListRow, { props: { conversation: CONVERSATION } })
    expect(wrapper.text()).toContain('Fix CI pipeline')
  })

  it('renders conversation id when name is null', () => {
    const noNameConversation = { ...CONVERSATION, name: null }
    const wrapper = mount(ConversationListRow, { props: { conversation: noNameConversation } })
    expect(wrapper.text()).toContain('1')
  })

  it('renders status pill with correct class', () => {
    const wrapper = mount(ConversationListRow, { props: { conversation: CONVERSATION } })
    const pill = wrapper.find('.status-pill')
    expect(pill.exists()).toBe(true)
    expect(pill.classes()).toContain('status-active')
  })

  it('renders archived status', () => {
    const archivedConversation = { ...CONVERSATION, status: 'archived' as const }
    const wrapper = mount(ConversationListRow, { props: { conversation: archivedConversation } })
    const pill = wrapper.find('.status-pill')
    expect(pill.classes()).toContain('status-archived')
  })

  it('applies selected class when conversation is selected in UI store', async () => {
    const { useUiStore } = await import('../../stores/ui')
    const uiStore = useUiStore()
    uiStore.selectConversation(1)

    const wrapper = mount(ConversationListRow, { props: { conversation: CONVERSATION } })
    expect(wrapper.find('.conversation-row').classes()).toContain('selected')
  })
})
