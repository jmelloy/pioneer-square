import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import GuildSidebar from '../GuildSidebar.vue'
import ConversationList from '../sidebar/ConversationList.vue'

const stubs = {
  ConversationList: { props: ['filtersCollapsed'], template: '<div />' },
  CostSummary: true,
}

function mountSidebar(mobile: boolean) {
  vi.stubGlobal('matchMedia', () => ({ matches: mobile }))
  return mount(GuildSidebar, { global: { stubs } })
}

describe('GuildSidebar filters toggle', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
  })
  afterEach(() => vi.unstubAllGlobals())

  it('defaults collapsed on mobile, expanded on desktop', () => {
    expect(mountSidebar(true).find('.filters-toggle').attributes('aria-expanded')).toBe('false')
    expect(mountSidebar(false).find('.filters-toggle').attributes('aria-expanded')).toBe('true')
  })

  it('toggles, passes state down, and persists the choice over the viewport default', async () => {
    const wrapper = mountSidebar(false)
    await wrapper.find('.filters-toggle').trigger('click')
    expect(wrapper.findComponent(ConversationList).props('filtersCollapsed')).toBe(true)
    expect(localStorage.getItem('conversationFiltersCollapsed')).toBe('true')
    expect(mountSidebar(false).find('.filters-toggle').attributes('aria-expanded')).toBe('false')
  })
})
