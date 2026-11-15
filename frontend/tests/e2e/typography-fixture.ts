import type { Page } from 'playwright'

const now = '2026-01-01T12:00:00Z'
export const prose = `# Review summary

## Findings

### Next steps

Readable prose with **important text**, a [link](https://example.com), and an identifier: ${'long-identifier'.repeat(20)}.

\`\`\`text
    ${'preserve_indentation '.repeat(30)}
\`\`\`

| Field | Value |
| --- | --- |
| Example | ${'wide-table-value'.repeat(12)} |`

export const task = {
  id: 't-typography',
  guild_id: 'type-detail',
  name: `Review ${'long-task-title'.repeat(6)}`,
  description: prose,
  state: 'awaiting-review',
  phase: 'execute',
  task_type: 'issue',
  worker_id: 'w-type',
  branch: `feature/${'long-branch-name'.repeat(8)}`,
  created_at: now,
  updated_at: now,
  issue_repo: 'test/factory',
  issue_number: 1,
}

export const logs = [
  { line: prose, timestamp: now, level: 'claude' },
  { line: `[worker]    ${'raw_stdout '.repeat(40)}`, timestamp: now, level: 'worker' },
  {
    line: '▶ bash: echo example',
    timestamp: now,
    level: 'tool',
    detail: { toolType: 'tool_use', name: 'bash', input: { command: '    echo example' } },
  },
]

type TaskNode = typeof task & { children: TaskNode[] }

const child = Array.from({ length: 6 }).reduce<TaskNode>(
  (node, _, index) => ({
    ...task,
    id: `t-parent-${index}`,
    name: `Subtask ${index}`,
    children: [node],
  }),
  { ...task, children: [] },
)

const guild = {
  id: task.guild_id,
  name: 'Typography checks',
  primary_repo: task.issue_repo,
  agents: [
    {
      id: 'a-type',
      name: 'Type Agent',
      type: 'worker',
      state: 'idle',
      worker_id: 'w-type',
      worker_name: 'Type Worker',
    },
  ],
  messages: [],
}

const issue = {
  id: 1,
  number: 1,
  title: 'Review the typography',
  state: 'open',
  labels: [{ name: 'readability', color: 'eab900' }],
  created_at: now,
  updated_at: now,
  user: { login: 'tester', avatar_url: '' },
  body: prose,
}

export async function installTypographyFixture(page: Page) {
  await page.addInitScript(() => {
    localStorage.clear()
    localStorage.setItem('auth_token', 'test-token')
    localStorage.setItem('auth_user', JSON.stringify({ login: 'tester', id: '1' }))
    localStorage.setItem('gh_token', 'test-token')
  })
  await page.routeWebSocket('**/ws/**', () => {})
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url())
    let data: unknown
    if (url.pathname === '/guilds') data = [guild]
    else if (url.pathname === `/guilds/${guild.id}`) data = guild
    else if (url.pathname === `/api/task/${task.id}/log`) data = { task, logs }
    else if (url.pathname.endsWith('/tasks/tree'))
      data = {
        conversations: [],
        ungrouped: [
          { ...task, id: 't-root', name: 'Issue root', phase: 'issue', children: [child] },
        ],
      }
    else if (url.pathname.endsWith('/tasks')) data = [task]
    else if (url.pathname.endsWith('/logs')) data = logs
    else if (url.pathname.endsWith('/usage'))
      data = [
        {
          task_id: task.id,
          kind: 'api_call',
          model: 'example-model',
          input_tokens: 10000000,
          output_tokens: 9999999,
          cache_read_input_tokens: 100000,
          cache_creation_input_tokens: 100000,
        },
      ]
    else if (url.pathname.endsWith('/foreman/context'))
      data = {
        system: 'Synthetic system prompt',
        messages: [
          {
            role: 'assistant',
            content: [
              { type: 'text', text: prose },
              { type: 'tool_use', name: 'example', input: { key: 'long-value'.repeat(30) } },
            ],
          },
        ],
      }
    else if (url.pathname.startsWith('/api/issues/'))
      data = { issue, comments: [{ id: 1, body: prose, created_at: now, user: issue.user }] }
    else if (url.hostname === 'api.github.com') data = [issue]
    else if (url.pathname === '/api/models') data = []
    else if (url.pathname.startsWith('/guilds/') || url.pathname.startsWith('/api/guilds/'))
      data = []
    if (data === undefined) await route.continue()
    else await route.fulfill({ json: data })
  })
}
