import type { LogDetail } from '../types'

// Format stored events too; keep the original payload available in the JSON viewer.
export function claudeEventSummary(detail?: LogDetail | null): string | undefined {
  if (detail?.toolType !== 'claude_json' || !detail.event || typeof detail.event !== 'object')
    return undefined
  const event = detail.event as Record<string, unknown>
  const text = (value: unknown) => (typeof value === 'string' ? value : '')
  const join = (...values: string[]) => values.filter(Boolean).join(' · ')

  switch (event.type) {
    case 'tool_progress':
      return join(
        `${text(event.tool_name) || 'Tool'} running`,
        typeof event.elapsed_time_seconds === 'number' ? `${event.elapsed_time_seconds}s` : '',
        event.heartbeat === true ? 'heartbeat' : '',
      )
    case 'rate_limit_event': {
      const info = event.rate_limit_info
      if (!info || typeof info !== 'object') return 'Rate limit update'
      const rate = info as Record<string, unknown>
      return join('Rate limit', text(rate.status), text(rate.rateLimitType))
    }
    case 'user': {
      const message = event.message
      if (!message || typeof message !== 'object') return undefined
      const content = (message as Record<string, unknown>).content
      const body = Array.isArray(content)
        ? content
            .map((block) =>
              block && typeof block === 'object' && block.type === 'text' ? text(block.text) : '',
            )
            .filter(Boolean)
            .join('\n')
        : text(content)
      return body ? join(event.isSynthetic === true ? 'Context' : 'User', body) : undefined
    }
    case 'system':
      switch (event.subtype) {
        case 'task_started':
          return join('Task started', text(event.description), text(event.task_id))
        case 'task_notification':
          return join('Task', text(event.status), text(event.summary), text(event.task_id))
        case 'task_updated':
          return join('Task updated', text(event.task_id), JSON.stringify(event.patch))
        case 'background_tasks_changed':
          return Array.isArray(event.tasks)
            ? join(
                `Background tasks: ${event.tasks.length}`,
                ...event.tasks.map((task) =>
                  task && typeof task === 'object' ? text(task.description) : '',
                ),
              )
            : 'Background tasks updated'
        case 'code_change_published':
          return join('Code change', text(event.action), text(event.repo), text(event.url))
        case 'vcs_state_changed':
          return join('Repository updated', text(event.kind), text(event.cwd))
        case 'informational':
          return join('Info', text(event.level), text(event.content))
      }
  }
  return undefined
}
