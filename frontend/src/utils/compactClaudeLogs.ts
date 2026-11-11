import type { LogEntry } from '../types'
import { claudeEventSummary } from './claudeEventSummary'

export interface IndexedLog {
  log: LogEntry
  index: number
}

export interface ClaudeLogGroup {
  type: 'event'
  key: string
  summary: string
  entries: IndexedLog[]
  priority: number
}

// This is a display projection: never discard the original entries or pair tools by adjacency.
export function compactClaudeLogs(logs: IndexedLog[]): (IndexedLog | ClaudeLogGroup)[] {
  const result: (IndexedLog | ClaudeLogGroup)[] = []
  const groups = new Map<string, ClaudeLogGroup>()
  const rateStates = new Map<string, { state: string; key: string }>()

  for (const entry of logs) {
    const detail = entry.log.detail
    const event = detail?.toolType === 'claude_json' ? detail.event : undefined
    if (!event || typeof event !== 'object') {
      result.push(entry)
      continue
    }
    const e = event as Record<string, unknown>
    let key: string | undefined
    let priority = 0
    const session = e.session_id
    if (typeof session === 'string' && session) {
      if (
        e.type === 'system' &&
        ['task_started', 'task_updated', 'task_notification'].includes(String(e.subtype)) &&
        typeof e.task_id === 'string' &&
        e.task_id
      ) {
        key = JSON.stringify([session, 'task', e.task_id])
        // Late progress must not overwrite a completion, especially a failure.
        priority = e.subtype === 'task_notification' ? (e.status === 'completed' ? 1 : 2) : 0
        if (e.subtype === 'task_updated' && e.patch && typeof e.patch === 'object') {
          const status = (e.patch as Record<string, unknown>).status
          if (status === 'completed') priority = 1
          else if (['failed', 'stopped', 'cancelled'].includes(String(status))) priority = 2
        }
      } else if (
        e.type === 'system' &&
        ['hook_started', 'hook_response'].includes(String(e.subtype)) &&
        typeof e.hook_id === 'string' &&
        e.hook_id &&
        typeof e.hook_event === 'string' &&
        e.hook_event
      ) {
        key = JSON.stringify([session, 'hooks', e.hook_event])
        priority =
          e.subtype === 'hook_response' ? (e.outcome === 'success' && e.exit_code === 0 ? 1 : 2) : 0
      } else if (
        e.type === 'tool_progress' &&
        e.heartbeat === true &&
        typeof e.parent_tool_use_id === 'string' &&
        e.parent_tool_use_id
      ) {
        key = JSON.stringify([session, 'heartbeat', e.parent_tool_use_id])
        priority = typeof e.elapsed_time_seconds === 'number' ? e.elapsed_time_seconds : 0
      } else if (e.type === 'rate_limit_event' && e.rate_limit_info) {
        const state = JSON.stringify(e.rate_limit_info)
        const previous = rateStates.get(session)
        key =
          previous?.state === state ? previous.key : JSON.stringify([session, 'rate', entry.index])
        rateStates.set(session, { state, key })
      }
    }
    const summary = claudeEventSummary(detail)
    if (!key || summary === undefined) {
      result.push(entry)
      continue
    }
    const group = groups.get(key)
    if (group) {
      group.entries.push(entry)
      if (priority >= group.priority) {
        group.summary = summary
        group.priority = priority
      }
    } else {
      const group: ClaudeLogGroup = { type: 'event', key, summary, entries: [entry], priority }
      groups.set(key, group)
      result.push(group)
    }
  }
  return result
}
