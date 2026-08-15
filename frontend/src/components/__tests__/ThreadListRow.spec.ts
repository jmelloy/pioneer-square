import { beforeEach, describe, expect, it } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { mount } from '@vue/test-utils'
import ThreadListRow from '../sidebar/ThreadListRow.vue'
import type { ConversationThread } from '../../types'

const THREAD: ConversationThread = {
  id: 'thr-001',
  conversation_id: 1,
  discord_thread_id: null,
  name: 'Fix CI pipeline',
  status: 'active',
  created_at: '2025-07-01T00:00:00Z',
  updated_at: '2025-07-01T00:00:00Z',
}

describe('ThreadListRow', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('renders thread name', () => {
    const wrapper = mount(ThreadListRow, { props: { thread: THREAD } })
    expect(wrapper.text()).toContain('Fix CI pipeline')
  })

  it('renders thread id when name is null', () => {
    const noNameThread = { ...THREAD, name: null }
    const wrapper = mount(ThreadListRow, { props: { thread: noNameThread } })
    expect(wrapper.text()).toContain('thr-001')
  })

  it('renders status pill with correct class', () => {
    const wrapper = mount(ThreadListRow, { props: { thread: THREAD } })
    const pill = wrapper.find('.status-pill')
    expect(pill.exists()).toBe(true)
    expect(pill.classes()).toContain('status-active')
  })

  it('renders archived status', () => {
    const archivedThread = { ...THREAD, status: 'archived' as const }
    const wrapper = mount(ThreadListRow, { props: { thread: archivedThread } })
    const pill = wrapper.find('.status-pill')
    expect(pill.classes()).toContain('status-archived')
  })

  it('applies selected class when thread is selected in UI store', async () => {
    const { useUiStore } = await import('../../stores/ui')
    const uiStore = useUiStore()
    uiStore.selectThread('thr-001')

    const wrapper = mount(ThreadListRow, { props: { thread: THREAD } })
    expect(wrapper.find('.thread-row').classes()).toContain('selected')
  })
})
