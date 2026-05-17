import { createApp, h, defineComponent } from 'vue'
import WorkbenchRow from '../src/components/factory-floor/WorkbenchRow.vue'
import type { Task, TaskState } from '../src/types'
import '../src/style.css'

const STATIONS = [
  { key: 'reading', label: 'ARCHIVE', frac: 0.1, component: 'archive' },
  { key: 'searching', label: 'SCRYING', frac: 0.25, component: 'orb' },
  { key: 'fetching', label: 'TELEGRAPH', frac: 0.4, component: 'telegraph' },
  { key: 'thinking', label: 'THINK TANK', frac: 0.56, component: 'tank' },
  { key: 'running', label: 'ENGINE', frac: 0.72, component: 'engine' },
  { key: 'editing', label: 'FORGE', frac: 0.88, component: 'forge' },
] as const

const STATES: TaskState[] = [
  'pending',
  'planning',
  'working',
  'awaiting-review',
  'followup',
  'done',
  'failed',
]

function buildTask(state: TaskState, idx: number): Task {
  return {
    id: `t-${idx}`,
    name: `Demo · ${state}`,
    state,
  }
}

const App = defineComponent({
  setup() {
    return () =>
      h(
        'div',
        {
          style: {
            background: '#120900',
            padding: '24px',
            display: 'flex',
            flexDirection: 'column',
            gap: '12px',
            fontFamily: 'monospace',
            color: '#e8d4a4',
            minHeight: '100vh',
          },
        },
        [
          h('div', { style: { fontSize: '14px', letterSpacing: '2px' } }, 'BENCH SCENERY DEMO'),
          h(
            'div',
            {
              style: { display: 'flex', flexDirection: 'column', gap: '12px', width: '760px' },
            },
            STATES.map((state, i) =>
              h('div', { id: `row-${state}`, style: { height: '92px' } }, [
                h(WorkbenchRow, {
                  task: buildTask(state, i),
                  index: i,
                  rowHeight: 92,
                  activityKey: null,
                  stations: STATIONS,
                  stateLabel: (s: string) => s.toUpperCase(),
                }),
              ]),
            ),
          ),
          // vacant row for reference
          h('div', { style: { height: '92px', width: '760px' } }, [
            h(WorkbenchRow, {
              task: null,
              index: 99,
              rowHeight: 92,
              activityKey: null,
              stations: STATIONS,
              stateLabel: (s: string) => s.toUpperCase(),
            }),
          ]),
        ],
      )
  },
})

createApp(App).mount('#app')
