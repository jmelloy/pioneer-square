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

describe('LogLine Claude event summaries', () => {
  it('shows progress inline and preserves the full payload on expansion', async () => {
    const event = { type: 'tool_progress', tool_name: 'Bash', elapsed_time_seconds: 42 }
    const log = makeLog({
      line: '[claude-json] tool_progress',
      detail: { toolType: 'claude_json', event },
    })
    const wrapper = mount(LogLine, { props: { log, expanded: false } })
    expect(wrapper.find('.log-text').text()).toBe('Bash running · 42s')
    await wrapper.find('.log-line').trigger('click')
    expect(wrapper.emitted('toggle')).toHaveLength(1)
    await wrapper.setProps({ expanded: true })
    expect(JSON.parse(wrapper.find('.log-detail-body').text())).toEqual(event)
  })

  it('escapes event content instead of interpreting HTML or markdown', () => {
    const log = makeLog({
      detail: {
        toolType: 'claude_json',
        event: {
          type: 'system',
          subtype: 'informational',
          content: '<img src=x onerror=alert(1)> **text**',
        },
      },
    })
    const wrapper = mount(LogLine, { props: { log, expanded: false } })
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.find('strong').exists()).toBe(false)
    expect(wrapper.find('.log-text').text()).toContain('<img src=x onerror=alert(1)> **text**')
  })
})

describe('LogLine Pi tools', () => {
  it.each([
    ['bash', { command: 'echo hello' }, ['COMMAND', 'echo hello']],
    [
      'edit',
      { path: 'a.ts', oldText: 'old code', newText: 'new code' },
      ['OLD', 'old code', 'NEW', 'new code'],
    ],
    ['write', { path: 'a.ts', content: 'file contents' }, ['a.ts', 'file contents']],
    ['read', { path: 'a.ts', offset: 10, limit: 20 }, ['FILE', 'a.ts', 'offset', 'limit']],
    ['subagent', { task: 'Run tests' }, ['subagent', 'Run tests']],
  ])('renders %s input without losing fields', (name, input, expected) => {
    const log = makeLog({ detail: { toolType: 'tool_use', name, input } })
    const wrapper = mount(LogLine, { props: { log, expanded: true } })
    for (const value of expected) expect(wrapper.find('.log-detail').text()).toContain(value)
  })

  it.each([
    ['  → first (+1 lines)', 'first\nsecond'],
    ['  → ' + 'x'.repeat(120), 'x'.repeat(160)],
  ])('allows expansion of truncated output: %s', (line, output) => {
    const log = makeLog({ line, detail: { toolType: 'tool_result', output } })
    const wrapper = mount(LogLine, { props: { log, expanded: true } })
    expect(wrapper.find('.log-expand-icon').exists()).toBe(true)
    expect(wrapper.find('.log-detail-body').text()).toBe(output)
  })

  it('does not expand output already displayed in full', () => {
    const log = makeLog({ line: '  → done', detail: { toolType: 'tool_result', output: 'done' } })
    expect(
      mount(LogLine, { props: { log, expanded: false } })
        .find('.log-expand-icon')
        .exists(),
    ).toBe(false)
  })

  it('shows Pi progress with expandable raw JSON', () => {
    const event = { type: 'tool_execution_update', toolName: 'bash', toolCallId: 'call-1' }
    const log = makeLog({ detail: { toolType: 'pi_json', event, output: 'Running tests' } })
    const wrapper = mount(LogLine, { props: { log, expanded: true } })
    expect(wrapper.find('.log-text').text()).toBe('bash running · Running tests')
    expect(JSON.parse(wrapper.find('.log-detail-body').text())).toEqual(event)
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
