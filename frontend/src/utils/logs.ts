import type { LogEntry } from '../types'

function nonEmptyString(value: unknown): boolean {
  return typeof value === 'string' && value.trim().length > 0
}

function hasContentBlock(value: unknown): boolean {
  if (nonEmptyString(value)) return true
  if (Array.isArray(value)) return value.some(hasContentBlock)
  if (!value || typeof value !== 'object') return false

  const record = value as Record<string, unknown>
  return (
    nonEmptyString(record.text) ||
    nonEmptyString(record.thinking) ||
    nonEmptyString(record.content) ||
    hasContentBlock(record.content) ||
    hasContentBlock(record.message) ||
    hasContentBlock(record.block)
  )
}

export function isVisibleLogEntry(log: LogEntry): boolean {
  if (!log.line) return false

  const detail = log.detail
  if (detail?.toolType !== 'claude_json') return true

  const event = detail.event
  if (!event || typeof event !== 'object') return true
  const record = event as Record<string, unknown>

  // Claude Code can emit bookkeeping-only stream events such as
  // `system:thinking_tokens`. When they carry no text/content blocks, the
  // generic `[claude-json] ...` fallback is just viewer noise.
  if (record.type === 'system' && record.subtype === 'thinking_tokens') {
    return hasContentBlock(record)
  }

  return true
}
