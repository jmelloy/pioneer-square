import { describe, expect, it } from 'vitest'
import { compactClaudeLogs, type ClaudeLogGroup } from '../compactClaudeLogs'
import type { LogEntry } from '../../types'

function eventLog(event: Record<string, unknown>): LogEntry {
  return {
    line: '[claude-json]',
    timestamp: '2026-01-01T00:00:00Z',
    detail: { toolType: 'claude_json', event: { session_id: 's-1', ...event } },
  }
}

function compact(logs: LogEntry[]) {
  return compactClaudeLogs(logs.map((log, index) => ({ log, index })))
}

function groups(logs: LogEntry[]): ClaudeLogGroup[] {
  return compact(logs).filter((item): item is ClaudeLogGroup => 'type' in item)
}

const started = {
  type: 'system',
  subtype: 'task_started',
  task_id: 'bg-1',
  description: 'Run tests',
}
const completed = {
  type: 'system',
  subtype: 'task_notification',
  task_id: 'bg-1',
  status: 'completed',
  summary: 'Passed',
}

describe('compactClaudeLogs', () => {
  it('pairs interleaved lifecycle events without losing entries or changing the input', () => {
    const logs = [eventLog(started), eventLog({ ...started, task_id: 'bg-2' }), eventLog(completed)]
    const result = groups(logs)
    expect(result).toHaveLength(2)
    expect(result[0].summary).toContain('completed · Passed')
    expect(result[0].entries.map((entry) => entry.index)).toEqual([0, 2])
    expect(result[0].entries[0].log).toBe(logs[0])
    expect(logs[0].detail?.event).toEqual({ session_id: 's-1', ...started })
  })

  it('keeps terminal failures visible despite late progress or completion', () => {
    const result = groups([
      eventLog(started),
      eventLog({ ...completed, status: 'failed', summary: 'Tests failed' }),
      eventLog(started),
      eventLog(completed),
    ])
    expect(result[0].summary).toContain('failed · Tests failed')
    expect(result[0].entries).toHaveLength(4)
  })

  it.each(['completed', 'failed'])('retains terminal task_updated status %s', (status) => {
    const result = groups([
      eventLog(started),
      eventLog({ type: 'system', subtype: 'task_updated', task_id: 'bg-1', patch: { status } }),
      eventLog(started),
    ])
    expect(result[0].summary).toContain(status)
    expect(result[0].entries).toHaveLength(3)
  })

  it('replaces synthetic heartbeat IDs by parent, keeping concurrent parents separate', () => {
    const heartbeat = {
      type: 'tool_progress',
      tool_name: 'Bash',
      parent_tool_use_id: 'call-1',
      heartbeat: true,
    }
    const result = groups([
      eventLog({ ...heartbeat, tool_use_id: 'call-1-heartbeat-0', elapsed_time_seconds: 30 }),
      eventLog({ ...heartbeat, parent_tool_use_id: 'call-2', elapsed_time_seconds: 30 }),
      eventLog({ ...heartbeat, tool_use_id: 'call-1-heartbeat-1', elapsed_time_seconds: 60 }),
      eventLog({ ...heartbeat, elapsed_time_seconds: 30 }),
    ])
    expect(result).toHaveLength(2)
    expect(result[0].summary).toContain('60s')
    expect(result[0].entries).toHaveLength(3)
  })

  it('does not merge separate sessions, missing IDs, or arbitrary child progress', () => {
    const logs = [
      eventLog(started),
      eventLog({ ...started, session_id: 's-2' }),
      eventLog({ ...started, task_id: undefined }),
      eventLog({ ...started, session_id: undefined }),
      eventLog({ type: 'tool_progress', parent_tool_use_id: 'agent-1', tool_use_id: 'child-1' }),
      eventLog({ type: 'tool_progress', parent_tool_use_id: 'agent-1', tool_use_id: 'child-2' }),
    ]
    expect(compact(logs)).toHaveLength(6)
  })

  it('deduplicates unchanged rate state without swallowing warning/recovery transitions', () => {
    const rate = (status: string) =>
      eventLog({ type: 'rate_limit_event', rate_limit_info: { status } })
    const result = groups([rate('allowed'), rate('allowed'), rate('rejected'), rate('allowed')])
    expect(result.map((group) => group.entries.length)).toEqual([2, 1, 1])
    expect(result[1].summary).toContain('rejected')
    expect(result[2].summary).toContain('allowed')
  })

  it('collapses setup hooks but never replaces a failure with a later success', () => {
    const hook = { type: 'system', hook_event: 'SessionStart', hook_id: 'hook-1' }
    const result = groups([
      eventLog({ ...hook, subtype: 'hook_started' }),
      eventLog({ ...hook, subtype: 'hook_response', outcome: 'error', exit_code: 1 }),
      eventLog({
        ...hook,
        hook_id: 'hook-2',
        subtype: 'hook_response',
        outcome: 'success',
        exit_code: 0,
      }),
    ])
    expect(result).toHaveLength(1)
    expect(result[0].summary).toContain('failed')
    expect(result[0].entries).toHaveLength(3)
  })

  it('preserves plain text and unknown events in order', () => {
    const logs = [eventLog({ type: 'new_event' }), { line: 'hello', timestamp: '' }]
    expect(compact(logs)).toEqual(logs.map((log, index) => ({ log, index })))
  })
})
