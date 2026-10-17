import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import ChatTab from '../chat-pane/ChatTab.vue'
import { useGuildStore } from '../../stores/guild'

describe('ChatTab conversation isolation', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('only renders unscoped messages while retaining conversation messages in the store', async () => {
    const guild = useGuildStore()
    guild.messages = [
      { type: 'chat', from: 'system', content: 'Guild announcement' },
      { type: 'chat', from: 'user', content: 'Conversation question', conversationId: 42 },
      { type: 'chat', from: 'foreman', content: 'Conversation answer', conversationId: 42 },
      { type: 'chat', from: 'foreman', content: 'Another conversation', conversationId: 43 },
    ]
    const wrapper = mount(ChatTab)
    expect(wrapper.text()).toContain('Guild announcement')
    expect(wrapper.text()).not.toContain('Conversation question')
    expect(wrapper.text()).not.toContain('Conversation answer')
    expect(wrapper.text()).not.toContain('Another conversation')
    expect(guild.messages).toHaveLength(4)

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
