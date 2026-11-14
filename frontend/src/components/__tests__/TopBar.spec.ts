import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import TopBar from '../TopBar.vue'
import { useUiStore } from '../../stores/ui'

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

describe('workspace navigation', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('switches screens without losing the open work tab', async () => {
    const ui = useUiStore()
    ui.openConversationTab(42)
    const wrapper = mount(TopBar)
    const buttons = wrapper.findAll('.workspace-nav button')
    expect(buttons[0]!.attributes('aria-pressed')).toBe('true')
    await buttons[1]!.trigger('click')
    expect(ui.workspace).toBe('projects')
    expect(buttons[1]!.attributes('aria-pressed')).toBe('true')
    await buttons[0]!.trigger('click')
    expect(ui.workspace).toBe('work')
    expect(ui.activeTab).toBe('conversation-42')
    wrapper.unmount()
  })
})
