import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import ChatTab from '../chat-pane/ChatTab.vue'
import { useGuildStore } from '../../stores/guild'
import { useUiStore } from '../../stores/ui'

describe('ChatTab conversation isolation', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('renders unscoped messages and only the top-level comment of each conversation', async () => {
    const guild = useGuildStore()
    guild.messages = [
      { type: 'chat', from: 'system', content: 'Guild announcement' },
      { type: 'chat', from: 'user', content: 'Conversation question', conversationId: 42 },
      { type: 'chat', from: 'foreman', content: 'Conversation answer', conversationId: 42 },
      { type: 'chat', from: 'foreman', content: 'Another conversation', conversationId: 43 },
    ]
    const wrapper = mount(ChatTab)
    expect(wrapper.text()).toContain('Guild announcement')
    expect(wrapper.text()).toContain('Conversation question')
    expect(wrapper.text()).toContain('Another conversation')
    expect(wrapper.text()).not.toContain('Conversation answer')
    expect(guild.messages).toHaveLength(4)
    expect(wrapper.findAll('.conversation-link')).toHaveLength(2)

    await wrapper.find('.conversation-link').trigger('click')
    expect(useUiStore().activeTab).toBe('conversation-42')

    guild.messages.push({
      type: 'chat',
      from: 'foreman',
      content: 'Live reply',
      conversationId: 42,
    })
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).not.toContain('Live reply')
    wrapper.unmount()
  })
})
