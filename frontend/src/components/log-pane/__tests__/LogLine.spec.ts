import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import LogLine from '../LogLine.vue'
import type { LogEntry } from '../../../types'

function makeLog(overrides: Partial<LogEntry> = {}): LogEntry {
  return {
    line: 'test line',
    timestamp: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

describe('LogLine fallback JSON viewer', () => {
  it('is expandable for unknown toolTypes', () => {
    const log = makeLog({
      line: '[claude-json] error:rate_limited',
      detail: {
        toolType: 'claude_json',
        event: { type: 'error', error: { type: 'rate_limit_error', message: 'Rate limited' } },
      },
    })
    const wrapper = mount(LogLine, { props: { log, expanded: false } })

    // Should show expand icon indicator
    expect(wrapper.find('.log-expand-icon').exists()).toBe(true)
    // Should be clickable (has expandable class)
    expect(wrapper.find('.log-line--expandable').exists()).toBe(true)
  })

  it('shows RAW JSON label when expanded for unknown toolType', async () => {
    const log = makeLog({
      line: '[claude-json] error:rate_limited',
      detail: {
        toolType: 'claude_json',
        event: { type: 'error', error: { type: 'rate_limit_error', message: 'Rate limited' } },
      },
    })
    const wrapper = mount(LogLine, { props: { log, expanded: true } })

    // Should show the raw JSON detail section
    expect(wrapper.find('.log-detail').exists()).toBe(true)
    expect(wrapper.find('.log-detail-label').text()).toBe('RAW JSON')
    // Should have the specialized raw styling
    expect(wrapper.find('.log-detail-raw').exists()).toBe(true)
  })

  it('formats claude_json event payload as prettified JSON when expanded', async () => {
    const log = makeLog({
      line: '[claude-json] error:rate_limited',
      detail: {
        toolType: 'claude_json',
        event: { type: 'error', error: { type: 'rate_limit_error', message: 'Rate limited' } },
      },
    })
    const wrapper = mount(LogLine, { props: { log, expanded: true } })

    const detailBody = wrapper.find('.log-detail-body')
    const text = detailBody.text()
    // Should be prettified JSON with proper indentation
    expect(text).toContain('"type": "error"')
    expect(text).toContain('"error"')
    expect(text).toContain('"rate_limit_error"')
    // Should be formatted with newlines (prettified)
    expect(text).toContain('\n')
  })

  it('shows full detail object when no event property', async () => {
    const log = makeLog({
      line: '[unknown-type] something weird',
      detail: {
        toolType: 'unknown_type' as LogEntry['detail'] extends { toolType?: infer T } ? T : never,
        customField: 'custom value',
        anotherField: 42,
      },
    })
    const wrapper = mount(LogLine, { props: { log, expanded: true } })

    const detailBody = wrapper.find('.log-detail-body')
    const text = detailBody.text()
    // Should show the full detail object
    expect(text).toContain('"toolType"')
    expect(text).toContain('"customField"')
    expect(text).toContain('custom value')
    expect(text).toContain('42')
  })

  it('handles log entry without detail property', async () => {
    const log = makeLog({
      line: 'plain message',
      detail: null,
    })
    const wrapper = mount(LogLine, { props: { log, expanded: false } })

    // Should NOT be expandable when there's no detail
    expect(wrapper.find('.log-line--expandable').exists()).toBe(false)
    expect(wrapper.find('.log-expand-icon').exists()).toBe(false)
  })

  it('renders known tool types normally without fallback', async () => {
    const toolUseLog = makeLog({
      line: '▶ Write',
      detail: {
        toolType: 'tool_use',
        name: 'Write',
        input: { file_path: '/src/test.ts', content: 'file content' },
      },
    })
    const wrapper = mount(LogLine, { props: { log: toolUseLog, expanded: true } })

    // Should show normal tool_use detail (file path for Write tool)
    expect(wrapper.find('.log-detail-label').text()).toBe('/src/test.ts')
    // Should NOT show RAW JSON label
    expect(wrapper.text()).not.toContain('RAW JSON')
  })
})

describe('LogLine expansion toggle', () => {
  it('emits toggle event when clicked on expandable line', async () => {
    const log = makeLog({
      line: '▶ Bash',
      detail: { toolType: 'tool_use', name: 'Bash', input: { command: 'echo test' } },
    })
    const wrapper = mount(LogLine, { props: { log, expanded: false } })

    await wrapper.find('.log-line').trigger('click')

    expect(wrapper.emitted('toggle')).toHaveLength(1)
  })

  it('does not emit toggle when clicking non-expandable line', async () => {
    const log = makeLog({
      line: 'plain text',
      detail: null,
    })
    const wrapper = mount(LogLine, { props: { log, expanded: false } })

    await wrapper.find('.log-line').trigger('click')

    expect(wrapper.emitted('toggle')).toBeUndefined()
  })
})
