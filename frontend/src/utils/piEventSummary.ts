import type { LogDetail } from '../types'

export function piEventSummary(detail?: LogDetail | null): string | undefined {
  if (detail?.toolType !== 'pi_json' || !detail.event || typeof detail.event !== 'object')
    return undefined
  const event = detail.event as Record<string, unknown>
  const text = (value: unknown) => (typeof value === 'string' ? value : '')
  const join = (...parts: string[]) => parts.filter(Boolean).join(' · ')
  switch (event.type) {
    case 'tool_execution_update':
      return join(`${text(event.toolName) || 'Tool'} running`, detail.output?.slice(0, 160) ?? '')
    case 'auto_retry_start':
      return join(
        'Retrying',
        `attempt ${event.attempt}/${event.maxAttempts}`,
        text(event.errorMessage),
      )
    case 'auto_retry_end':
      return join(event.success ? 'Retry succeeded' : 'Retry failed', text(event.finalError))
    case 'compaction_start':
      return join('Compacting context', text(event.reason))
    case 'compaction_end':
      return join(
        event.aborted
          ? 'Compaction aborted'
          : event.errorMessage
            ? 'Compaction failed'
            : 'Context compacted',
        text(event.errorMessage),
      )
    case 'extension_error':
      return join('Extension error', text(event.event), text(event.error))
  }
  return undefined
}
