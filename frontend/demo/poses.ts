import { createApp, h, defineComponent } from 'vue'
import AgentAvatar from '../src/components/AgentAvatar.vue'
import type { Agent } from '../src/types'
import type { AgentPose } from '../src/composables/useErrands'
import '../src/style.css'

const POSES: AgentPose[] = ['stand', 'sit', 'lean']

function buildAgent(name: string, i: number): Agent {
  return {
    id: `a-${name}-${i}`,
    name,
    type: 'worker',
    workerId: `w-${i}`,
    state: 'idle',
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
            'IDLE-AGENT POSES — STAND · SIT (couch) · LEAN (table)',
          ),
          h(
            'div',
            {
              style: { display: 'flex', gap: '48px', flexWrap: 'wrap', alignItems: 'flex-end' },
            },
            POSES.map((p, i) =>
              h(
                'div',
                {
                  id: `cell-${p}`,
                  style: {
                    display: 'flex',
                    flexDirection: 'column',
                    alignItems: 'center',
                    gap: '8px',
                    width: '90px',
                  },
                },
                [
                  h(
                    'div',
                    { style: { fontSize: '8px', letterSpacing: '2px', color: '#e8aa00' } },
                    p.toUpperCase(),
                  ),
                  h(AgentAvatar, {
                    agent: buildAgent(p, i),
                    walking: false,
                    pose: p,
                  }),
                ],
              ),
            ),
          ),
        ],
      )
  },
})

createApp(App).mount('#app')
