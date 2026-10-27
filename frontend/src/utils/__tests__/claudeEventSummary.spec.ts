import { describe, expect, it } from 'vitest'
import { claudeEventSummary } from '../claudeEventSummary'

describe('claudeEventSummary', () => {
  it.each([
    [
      { type: 'tool_progress', tool_name: 'Bash', elapsed_time_seconds: 42, heartbeat: true },
      'Bash running · 42s · heartbeat',
    ],
    [{ type: 'tool_progress', tool_name: 'Read', elapsed_time_seconds: 0 }, 'Read running · 0s'],
    [
      {
        type: 'rate_limit_event',
        rate_limit_info: { status: 'allowed', rateLimitType: 'five_hour' },
      },
      'Rate limit · allowed · five_hour',
    ],
    [
      { type: 'system', subtype: 'task_started', description: 'Run tests', task_id: 'bg-1' },
      'Task started · Run tests · bg-1',
    ],
    [
      {
        type: 'system',
        subtype: 'task_notification',
        status: 'completed',
        summary: 'Tests passed',
        task_id: 'bg-1',
      },
      'Task · completed · Tests passed · bg-1',
    ],
    [
      {
        type: 'system',
        subtype: 'task_updated',
        task_id: 'bg-1',
        patch: { is_backgrounded: true },
      },
      'Task updated · bg-1 · {"is_backgrounded":true}',
    ],
    [
      {
        type: 'system',
        subtype: 'background_tasks_changed',
        tasks: [{ description: 'Run tests' }],
      },
      'Background tasks: 1 · Run tests',
    ],
    [{ type: 'system', subtype: 'background_tasks_changed', tasks: [] }, 'Background tasks: 0'],
    [
      {
        type: 'system',
        subtype: 'code_change_published',
        action: 'created',
        repo: 'org/repo',
        url: 'https://github.com/org/repo/pull/1',
      },
      'Code change · created · org/repo · https://github.com/org/repo/pull/1',
    ],
    [
      { type: 'system', subtype: 'vcs_state_changed', kind: 'branch', cwd: '/repo' },
      'Repository updated · branch · /repo',
    ],
    [
      {
        type: 'system',
        subtype: 'informational',
        level: 'warning',
        content: 'Check configuration',
      },
      'Info · warning · Check configuration',
    ],
    [
      {
        type: 'user',
        isSynthetic: true,
        message: { content: [{ type: 'text', text: 'Context update' }] },
      },
      'Context · Context update',
    ],
    [{ type: 'user', message: { content: 'Hello' } }, 'User · Hello'],
    [{ type: 'tool_progress' }, 'Tool running'],
    [{ type: 'rate_limit_event', rate_limit_info: null }, 'Rate limit update'],
    [{ type: 'system', subtype: 'background_tasks_changed', tasks: [null] }, 'Background tasks: 1'],
  ])('formats %j', (event, expected) => {
    expect(claudeEventSummary({ toolType: 'claude_json', event })).toBe(expected)
  })

  it.each([
    undefined,
    null,
    { toolType: 'tool_use' as const, name: 'Bash' },
    { toolType: 'claude_json' as const, event: null },
    { toolType: 'claude_json' as const, event: { type: 'new_event' } },
    { toolType: 'claude_json' as const, event: { type: 'system', subtype: 'new_subtype' } },
    { toolType: 'claude_json' as const, event: { type: 'user', message: {} } },
  ])('preserves the fallback for %j', (detail) => {
    expect(claudeEventSummary(detail)).toBeUndefined()
  })
})
