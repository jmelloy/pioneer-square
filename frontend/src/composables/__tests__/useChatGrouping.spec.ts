import { describe, expect, it } from 'vitest'
import { ref } from 'vue'
import { useChatGrouping, isToolUseGroup } from '../useChatGrouping'
import type { ChatMessage } from '../../types'

function msg(overrides: Partial<ChatMessage>): ChatMessage {
  return { type: 'chat', from: 'foreman', content: '', ...overrides }
}

describe('useChatGrouping', () => {
  it('keeps a plain foreman message and its taskId badge', () => {
    const messages = ref<ChatMessage[]>([msg({ content: 'on it', taskId: 't-abc' })])
    const grouped = useChatGrouping(messages)
    expect(grouped.value).toHaveLength(1)
    const only = grouped.value[0]
    expect(isToolUseGroup(only)).toBe(false)
    expect((only as ChatMessage).taskId).toBe('t-abc')
  })

  it('propagates the taskId from the first tool_use into the group', () => {
    const messages = ref<ChatMessage[]>([
      msg({ role: 'tool_use', toolName: 'send_followup', toolId: 'tu-1', taskId: 't-abc' }),
      msg({ role: 'tool_use', toolName: 'get_pr_status', toolId: 'tu-2', taskId: 't-abc' }),
    ])
    const grouped = useChatGrouping(messages)
    expect(grouped.value).toHaveLength(1)
    const group = grouped.value[0]
    expect(isToolUseGroup(group)).toBe(true)
    if (isToolUseGroup(group)) {
      expect(group.tools).toEqual(['send_followup', 'get_pr_status'])
      expect(group.taskId).toBe('t-abc')
    }
  })

  it('leaves the group taskId null for a parent-context tool call', () => {
    const messages = ref<ChatMessage[]>([
      msg({ role: 'tool_use', toolName: 'create_task', toolId: 'tu-1' }),
    ])
    const grouped = useChatGrouping(messages)
    const group = grouped.value[0]
    expect(isToolUseGroup(group)).toBe(true)
    if (isToolUseGroup(group)) {
      expect(group.taskId).toBeNull()
    }
  })
})
