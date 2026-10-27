import { describe, expect, it } from 'vitest'
import { piEventSummary } from '../piEventSummary'

describe('piEventSummary', () => {
  it.each([
    [{ type: 'tool_execution_update', toolName: 'bash' }, 'bash running'],
    [
      { type: 'auto_retry_start', attempt: 1, maxAttempts: 3, errorMessage: 'overloaded' },
      'Retrying · attempt 1/3 · overloaded',
    ],
    [{ type: 'auto_retry_end', success: true }, 'Retry succeeded'],
    [{ type: 'auto_retry_end', success: false, finalError: 'offline' }, 'Retry failed · offline'],
    [{ type: 'compaction_start', reason: 'threshold' }, 'Compacting context · threshold'],
    [{ type: 'compaction_end' }, 'Context compacted'],
    [{ type: 'compaction_end', aborted: true }, 'Compaction aborted'],
    [{ type: 'compaction_end', errorMessage: 'offline' }, 'Compaction failed · offline'],
    [
      { type: 'extension_error', event: 'tool_call', error: 'failed' },
      'Extension error · tool_call · failed',
    ],
    [{ type: 'future_event' }, undefined],
  ])('formats %j', (event, expected) => {
    expect(piEventSummary({ toolType: 'pi_json', event })).toBe(expected)
  })

  it('leaves non-Pi entries unchanged', () => {
    expect(piEventSummary(null)).toBeUndefined()
    expect(piEventSummary({ toolType: 'claude_json', event: {} })).toBeUndefined()
  })
})
