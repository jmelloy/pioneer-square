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

describe('LogLine unknown message type fallback', () => {
  it('is expandable for an unknown toolType', async () => {
    const log = makeLog({
      line: '[rate-limit] limit reached',
      detail: {
        toolType: 'rate_limit',
        retryAfter: 30,
        message: 'Rate limit exceeded',
      },
    })
    const wrapper = mount(LogLine, {
      props: { log, expanded: false },
    })

    // Should be marked as expandable
    expect(wrapper.find('.log-line--expandable').exists()).toBe(true)
  })

  it('is expandable for claude_json toolType', async () => {
    const log = makeLog({
      line: '[claude-json] system:thinking_tokens',
      detail: {
        toolType: 'claude_json',
        event: { type: 'system', subtype: 'thinking_tokens', tokens: 123 },
      },
    })
    const wrapper = mount(LogLine, {
      props: { log, expanded: false },
    })

    // Should be marked as expandable
    expect(wrapper.find('.log-line--expandable').exists()).toBe(true)
  })

  it('shows raw JSON when expanded for unknown toolType', async () => {
    const log = makeLog({
      line: '[rate-limit] limit reached',
      detail: {
        toolType: 'rate_limit',
        retryAfter: 30,
        message: 'Rate limit exceeded',
      },
    })
    const wrapper = mount(LogLine, {
      props: { log, expanded: true },
    })

    // Should show the raw JSON detail section
    expect(wrapper.find('.log-detail').exists()).toBe(true)
    expect(wrapper.find('.log-detail-label').text()).toBe('RAW JSON')

    const body = wrapper.find('.log-detail-body')
    expect(body.exists()).toBe(true)
    // Verify it's valid JSON with our fields
    const parsed = JSON.parse(body.text()!)
    expect(parsed.toolType).toBe('rate_limit')
    expect(parsed.retryAfter).toBe(30)
    expect(parsed.message).toBe('Rate limit exceeded')
  })

  it('shows raw JSON when expanded for claude_json toolType', async () => {
    const log = makeLog({
      line: '[claude-json] message.stop',
      detail: {
        toolType: 'claude_json',
        event: {
          type: 'message_stop',
          message: { id: 'msg_123', content: 'done' },
        },
      },
    })
    const wrapper = mount(LogLine, {
      props: { log, expanded: true },
    })

    expect(wrapper.find('.log-detail').exists()).toBe(true)
    expect(wrapper.find('.log-detail-label').text()).toBe('RAW JSON')

    const body = wrapper.find('.log-detail-body')
    const parsed = JSON.parse(body.text()!)
    expect(parsed.toolType).toBe('claude_json')
    expect(parsed.event.type).toBe('message_stop')
  })

  it('toggles expansion on click', async () => {
    const log = makeLog({
      line: '[unknown-event] something happened',
      detail: {
        toolType: 'unknown',
        data: { foo: 'bar' },
      },
    })

    const wrapper = mount(LogLine, {
      props: { log, expanded: false },
    })

    // Initially not expanded
    expect(wrapper.find('.log-detail').exists()).toBe(false)

    // Click to expand
    await wrapper.find('.log-line').trigger('click')
    expect(wrapper.emitted('toggle')).toHaveLength(1)
  })

  it('is not expandable when there is no detail', async () => {
    const log = makeLog({ line: 'plain text line' })
    const wrapper = mount(LogLine, {
      props: { log, expanded: false },
    })

    expect(wrapper.find('.log-line--expandable').exists()).toBe(false)
  })
})
