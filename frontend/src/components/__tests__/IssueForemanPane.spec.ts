import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import IssueForemanPane from '../IssueForemanPane.vue'
import { useConversationsStore } from '../../stores/conversations'
import { useGuildStore } from '../../stores/guild'
import type { Conversation } from '../../types'

enableAutoUnmount(afterEach)

function conv(id: number, name: string | null, updated: string): Conversation {
  return {
    id,
    name,
    user_id: 'u1',
    discord_thread_id: null,
    status: 'active',
    created_at: updated,
    updated_at: updated,
  }
}

describe('IssueForemanPane', () => {
  let guild: ReturnType<typeof useGuildStore>
  let convs: ReturnType<typeof useConversationsStore>

  beforeEach(() => {
    setActivePinia(createPinia())
    guild = useGuildStore()
    guild.currentGuild = { id: 'g1' }
    convs = useConversationsStore()
    vi.spyOn(convs, 'fetchConversationMessages').mockResolvedValue([])
  })
  afterEach(() => vi.restoreAllMocks())

  function render() {
    return mount(IssueForemanPane, {
      props: {
        owner: 'o',
        repo: 'r',
        issueNumber: 15,
        tasks: [{ id: 't-1', state: 'done', conversation_id: 7 }],
      },
    })
  }

  it('links searched, task and ref-named conversations and asks into the newest', async () => {
    const search = vi
      .spyOn(convs, 'searchConversations')
      .mockResolvedValue([conv(3, 'old', '2026-01-01T00:00:00Z')])
    convs.conversations = [
      conv(7, 'task conv', '2026-01-02T00:00:00Z'),
      conv(9, '[o/r#15] fresh', '2026-01-03T00:00:00Z'),
      conv(10, '[o/r#151] other issue', '2026-01-04T00:00:00Z'),
    ]
    const post = vi.spyOn(convs, 'postConversationMessage').mockResolvedValue({} as never)
    const w = render()
    await flushPromises()

    expect(search).toHaveBeenCalledWith('g1', '[o/r#15]')
    expect(w.findAll('.ifp-conv-name').map((e) => e.text())).toEqual([
      '[o/r#15] fresh',
      'task conv',
      'old',
    ])

    await w.find('textarea').setValue('status?')
    await w.find('form').trigger('submit')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('g1', 9, '[o/r#15] status?')
  })

  it('start conversation sends a top-level WS chat tagged with the issue', async () => {
    vi.spyOn(convs, 'searchConversations').mockResolvedValue([])
    const sendWs = vi.spyOn(guild, 'sendMessage').mockReturnValue(true)
    const w = render()
    await flushPromises()

    await w.findAll('.ifp-mode')[0]!.trigger('click')
    await w.find('textarea').setValue('kick off')
    await w.find('form').trigger('submit')
    expect(sendWs).toHaveBeenCalledWith({
      type: 'chat',
      from: 'user',
      to: 'foreman',
      content: '[o/r#15] kick off',
    })
  })
})
