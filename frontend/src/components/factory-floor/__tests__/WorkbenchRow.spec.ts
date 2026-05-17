import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import WorkbenchRow from '../WorkbenchRow.vue'
import type { Task, TaskState } from '../../../types'

const STATIONS = [
  { key: 'reading', label: 'ARCHIVE', frac: 0.1, component: 'archive' },
  { key: 'searching', label: 'SCRYING', frac: 0.25, component: 'orb' },
] as const

function buildTask(state: TaskState): Task {
  return {
    id: 't-1',
    name: 'Demo task',
    state,
  }
}

function mountRow(task: Task | null) {
  return mount(WorkbenchRow, {
    props: {
      task,
      index: 0,
      rowHeight: 90,
      activityKey: null,
      stations: STATIONS,
      stateLabel: (s: string) => s.toUpperCase(),
    },
  })
}

describe('WorkbenchRow scenery', () => {
  it('omits the scenery layer when the row is vacant', () => {
    const wrapper = mountRow(null)
    expect(wrapper.find('svg.bench-scene').exists()).toBe(false)
  })

  const cases: Array<[TaskState, string]> = [
    ['pending', 'scene-pending'],
    ['planning', 'scene-planning'],
    ['working', 'scene-working'],
    ['awaiting-review', 'scene-awaiting-review'],
    ['followup', 'scene-followup'],
    ['done', 'scene-done'],
    ['failed', 'scene-failed'],
    ['cancelled', 'scene-failed'],
  ]
  it.each(cases)('renders scenery for state %s with class %s', (state, klass) => {
    const wrapper = mountRow(buildTask(state))
    const svg = wrapper.find('svg.bench-scene')
    expect(svg.exists()).toBe(true)
    expect(svg.classes()).toContain(klass)
  })

  it('applies pixel-art crisp rendering on the scenery SVG', () => {
    const wrapper = mountRow(buildTask('working'))
    const svg = wrapper.find('svg.bench-scene')
    expect(svg.attributes('shape-rendering')).toBe('crispEdges')
  })
})
