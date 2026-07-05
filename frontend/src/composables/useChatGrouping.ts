import { computed, type ComputedRef, type Ref } from 'vue'
import type { ChatMessage } from '../types'

export interface ToolUseGroup {
  _isToolUseGroup: true
  tools: string[]
  pending: boolean
  from: string
  createdAt?: string
  created_at?: string
  // Carried from the first tool_use in the group so a child-context turn's
  // tool calls badge the same as its narration lines.
  taskId?: string | null
}

export type GroupedMessage = ChatMessage | ToolUseGroup

export function isToolUseGroup(msg: GroupedMessage): msg is ToolUseGroup {
  return (msg as ToolUseGroup)._isToolUseGroup === true
}

export function useChatGrouping(
  messages: Ref<ChatMessage[]> | ComputedRef<ChatMessage[]>,
): ComputedRef<GroupedMessage[]> {
  return computed((): GroupedMessage[] => {
    const raw = messages.value

    // Tool-result messages aren't rendered directly — their arrival flips the
    // matching tool-use group from "pending" to "complete" so the typing
    // indicator disappears without dumping raw output into the chat.
    const completedToolIds = new Set<string>()
    for (const msg of raw) {
      if (msg.role === 'tool_result' && msg.toolId) {
        completedToolIds.add(msg.toolId as string)
      }
    }

    const result: GroupedMessage[] = []
    let i = 0
    while (i < raw.length) {
      const msg = raw[i]
      if (msg.role === 'tool_use') {
        const tools: string[] = []
        const toolIds: string[] = []
        const first = msg
        while (i < raw.length && raw[i].role === 'tool_use') {
          tools.push((raw[i].toolName as string | undefined) || 'unknown')
          toolIds.push((raw[i].toolId as string | undefined) || '')
          i++
        }
        const pending = toolIds.some((id) => !id || !completedToolIds.has(id))
        result.push({
          _isToolUseGroup: true,
          tools,
          pending,
          from: (first.from || first.from_agent || 'foreman') as string,
          createdAt: first.createdAt,
          created_at: first.created_at,
          taskId: (first.taskId as string | null | undefined) ?? null,
        })
      } else if (msg.role === 'tool_result') {
        i++
      } else {
        result.push(msg)
        i++
      }
    }
    return result
  })
}
