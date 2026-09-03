import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import TaskLogView from '../TaskLogView.vue'
import { useAuthStore } from '../../stores/auth'

const TASK = {
  id: 't-abc123',
  name: 'Add the log viewer',
  description: 'do it',
  guild_id: 'g1',
  worker_id: 'w-vd3566',
  state: 'done',
  phase: 'execute',
  tool: 'claude',
  model: null,
  branch: 'feat/log-viewer',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T01:00:00Z',
  issue_number: 1269,
  issue_repo: 'jmelloy/pioneer-square',
  issue_title: 'Add a log viewer',
  pr_url: 'https://github.com/jmelloy/pioneer-square/pull/42',
}

const LOGS = [
  { line: 'cloning repo', timestamp: '2026-01-01T00:00:10Z' },
  { line: 'opened a PR', timestamp: '2026-01-01T01:00:00Z' },
]

function mockFetch(status = 200, body: unknown = { task: TASK, logs: LOGS }) {
  return vi.spyOn(globalThis, 'fetch').mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response)
}

function mountView() {
  const auth = useAuthStore()
  auth.loginToken = 'tok'
  return mount(TaskLogView)
}

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: 't-abc123' }, query: {} }),
  useRouter: () => ({ push: vi.fn() }),
}))

describe('TaskLogView', () => {
  beforeEach(() => setActivePinia(createPinia()))
  afterEach(() => vi.restoreAllMocks())

  it('renders task metadata and every log line', async () => {
    mockFetch()
    const wrapper = mountView()
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain('Add the log viewer')
    expect(text).toContain('t-abc123')
    expect(text).toContain('w-vd3566')
    expect(text).toContain('done')
    expect(text).toContain('execute')
    expect(text).toContain('jmelloy/pioneer-square#1269')
    expect(text).toContain('cloning repo')
    expect(text).toContain('opened a PR')
    // Timestamps come from LogLine's formatClock (HH:MM:SS).
    expect(text).toMatch(/\d{2}:\d{2}:\d{2}/)
  })

  it('links the issue and the PR', async () => {
    mockFetch()
    const wrapper = mountView()
    await flushPromises()

    const hrefs = wrapper.findAll('a').map((a) => a.attributes('href'))
    expect(hrefs).toContain('https://github.com/jmelloy/pioneer-square/issues/1269')
    expect(hrefs).toContain('https://github.com/jmelloy/pioneer-square/pull/42')
  })

  it('copies the current URL to the clipboard', async () => {
    mockFetch()
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('.copy-btn').trigger('click')
    expect(writeText).toHaveBeenCalledWith(window.location.href)
  })

  it('shows a not-found message for a missing task', async () => {
    mockFetch(404, { detail: 'Task not found' })
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('t-abc123 not found')
  })

  it('offers sign-in when unauthenticated', async () => {
    mockFetch(401, { detail: 'Authentication required' })
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('Sign in')
  })
})
