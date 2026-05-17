import { createApp, h, defineComponent } from 'vue'
import AgentAvatar from '../src/components/AgentAvatar.vue'
import type { Agent, AgentState } from '../src/types'
import '../src/style.css'

const STATES: AgentState[] = ['idle', 'thinking', 'working', 'busy', 'error']

function buildAgent(state: AgentState, type: 'worker' | 'foreman', i: number): Agent {
  return {
    id: `a-${state}-${i}`,
    name: `${state.slice(0, 3)}${i}`.toUpperCase(),
    type,
    workerId: `w-demo-${i}`,
    state,
    activity: null,
    logs: [],
    joinedAt: new Date().toISOString(),
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
            padding: '32px',
            minHeight: '100vh',
            color: '#e8d4a4',
            fontFamily: 'monospace',
          },
        },
        [
          h(
            'div',
            { style: { fontSize: '14px', letterSpacing: '2px', marginBottom: '24px' } },
            'PIXEL-ART ROBOT — STATES × {WORKER, FOREMAN} × {STILL, WALKING}',
          ),
          ...['worker', 'foreman'].map((t) =>
            h(
              'div',
              { style: { marginBottom: '32px' } },
              [
                h(
                  'div',
                  {
                    style: {
                      fontSize: '10px',
                      letterSpacing: '2px',
                      marginBottom: '12px',
                      color: '#e8aa00',
                    },
                  },
                  String(t).toUpperCase(),
                ),
                h(
                  'div',
                  {
                    style: {
                      display: 'flex',
                      gap: '24px',
                      flexWrap: 'wrap',
                      alignItems: 'flex-start',
                    },
                  },
                  STATES.flatMap((s, i) => [
                    h(
                      'div',
                      {
                        id: `cell-${t}-${s}-still`,
                        style: {
                          display: 'flex',
                          flexDirection: 'column',
                          alignItems: 'center',
                          gap: '6px',
                          width: '70px',
                        },
                      },
                      [
                        h(
                          'div',
                          { style: { fontSize: '7px', letterSpacing: '1px', opacity: 0.7 } },
                          s,
                        ),
                        h(AgentAvatar, {
                          agent: buildAgent(s, t as 'worker' | 'foreman', i),
                          walking: false,
                        }),
                      ],
                    ),
                    h(
                      'div',
                      {
                        id: `cell-${t}-${s}-walk`,
                        style: {
                          display: 'flex',
                          flexDirection: 'column',
                          alignItems: 'center',
                          gap: '6px',
                          width: '70px',
                        },
                      },
                      [
                        h(
                          'div',
                          { style: { fontSize: '7px', letterSpacing: '1px', opacity: 0.7 } },
                          `${s} walk`,
                        ),
                        h(AgentAvatar, {
                          agent: buildAgent(s, t as 'worker' | 'foreman', i + 100),
                          walking: true,
                        }),
                      ],
                    ),
                  ]),
                ),
              ],
            ),
          ),
        ],
      )
  },
})

createApp(App).mount('#app')
