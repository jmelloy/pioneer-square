import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import LogList from '../LogList.vue'
import type { LogEntry } from '../../../types'

function toolLog(name: string, toolType: 'tool_use' | 'tool_result' = 'tool_use'): LogEntry {
  return {
    line: `▶ ${name}`,
    timestamp: '2026-01-01T00:00:00Z',
    detail: toolType === 'tool_result' ? { toolType } : { toolType, name },
  }
}

function textLog(line: string): LogEntry {
  return { line, timestamp: '2026-01-01T00:00:00Z' }
}

describe('LogList turn grouping', () => {
  it('renders plain lines normally when there are no tool calls', () => {
    const logs = [textLog('hello'), textLog('world')]
    const wrapper = mount(LogList, { props: { logs } })

    expect(wrapper.text()).toContain('hello')
    expect(wrapper.text()).toContain('world')
    expect(wrapper.find('.turn-summary').exists()).toBe(false)
  })

  it('groups a run of consecutive tool_use/tool_result entries into one collapsed turn', () => {
    const logs = [
      textLog('starting up'),
      toolLog('Edit', 'tool_use'),
      toolLog('Edit', 'tool_result'),
      toolLog('Bash', 'tool_use'),
      toolLog('Bash', 'tool_result'),
      textLog('all done'),
    ]
    const wrapper = mount(LogList, { props: { logs } })

    const summary = wrapper.find('.turn-summary')
    expect(summary.exists()).toBe(true)
    expect(summary.text()).toContain('Turn 1')
    expect(summary.text()).toContain('edit × 1')
    expect(summary.text()).toContain('bash × 1')
    expect(summary.text()).toContain('2 tool calls')

    // Collapsed by default: no full log lines for the grouped tool entries.
    expect(wrapper.find('.turn-body').exists()).toBe(false)
  })

  it('expands a turn to reveal its full log lines on click', async () => {
    const logs = [
      toolLog('Read', 'tool_use'),
      toolLog('Read', 'tool_result'),
      toolLog('Bash', 'tool_use'),
      toolLog('Bash', 'tool_result'),
    ]
    const wrapper = mount(LogList, { props: { logs } })

    expect(wrapper.find('.turn-body').exists()).toBe(false)
    await wrapper.find('.turn-summary').trigger('click')
    expect(wrapper.find('.turn-body').exists()).toBe(true)
  })

  it('renders a single tool call directly instead of collapsing it into a turn', () => {
    const logs = [toolLog('Read', 'tool_use'), toolLog('Read', 'tool_result')]
    const wrapper = mount(LogList, { props: { logs } })

    expect(wrapper.find('.turn-summary').exists()).toBe(false)
    expect(wrapper.text()).toContain('▶ Read')
    // Both entries (tool_use + tool_result) should render inline, not be
    // swallowed by an accidental turn summary rendering alongside them.
    expect(wrapper.findAll('.log-line')).toHaveLength(2)
  })

  it('numbers multiple turns sequentially, separated by non-tool lines', () => {
    const logs = [
      toolLog('Edit', 'tool_use'),
      toolLog('Edit', 'tool_result'),
      toolLog('Write', 'tool_use'),
      toolLog('Write', 'tool_result'),
      textLog('thinking about next step'),
      toolLog('Bash', 'tool_use'),
      toolLog('Bash', 'tool_result'),
      toolLog('Read', 'tool_use'),
      toolLog('Read', 'tool_result'),
    ]
    const wrapper = mount(LogList, { props: { logs } })

    const summaries = wrapper.findAll('.turn-summary')
    expect(summaries).toHaveLength(2)
    expect(summaries[0].text()).toContain('Turn 1')
    expect(summaries[1].text()).toContain('Turn 2')
  })
})
