import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import SpawnWorkerForm from '../SpawnWorkerForm.vue'
import { useAuthStore } from '../../../stores/auth'
import { useGuildStore } from '../../../stores/guild'
import { useGitHubStore } from '../../../stores/github'

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response
}

interface Routes {
  credentials?: unknown
  settings?: unknown
  defaults?: unknown
}

function mockFetch(routes: Routes, onSpawn?: (body: unknown) => unknown) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
    const url = String(input)
    if (url.includes('/spawn-credentials')) {
      return Promise.resolve(jsonResponse(routes.credentials ?? { guild_env_vars: [] }))
    }
    if (url.includes('/spawn-worker')) {
      const body = init?.body ? JSON.parse(init.body as string) : {}
      return Promise.resolve(jsonResponse(onSpawn ? onSpawn(body) : { worker_id: 'w1' }))
    }
    if (url.includes('/spawn-settings')) return Promise.resolve(jsonResponse(routes.settings ?? {}))
    if (url.includes('/spawn-defaults')) return Promise.resolve(jsonResponse(routes.defaults ?? {}))
    return Promise.resolve(jsonResponse({}))
  })
}

function mountForm(repos: { full_name: string }[] = [{ full_name: 'org/repo' }]) {
  const auth = useAuthStore()
  auth.user = { id: 'u1', login: 'me' }
  auth.loginToken = 'tok'
  const guild = useGuildStore()
  guild.currentGuild = { id: 'g1', name: 'Guild', primary_repo: null }
  const gh = useGitHubStore()
  gh.repos = repos
  return mount(SpawnWorkerForm)
}

async function selectFirstRepo(wrapper: ReturnType<typeof mountForm>) {
  const repoCheckboxes = wrapper.findAll('.spawn-repo-list input[type="checkbox"]')
  // Index 0 is the "select all" org checkbox; index 1 is the repo itself.
  await repoCheckboxes[1].setValue(true)
}

async function switchTab(wrapper: ReturnType<typeof mountForm>, label: string) {
  const tabs = wrapper.findAll('.settings-tab')
  const tab = tabs.find((t) => t.text() === label)
  if (!tab) throw new Error(`Tab "${label}" not found`)
  await tab.trigger('click')
}

describe('SpawnWorkerForm credentials', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows masked guild env vars', async () => {
    mockFetch({
      credentials: {
        guild_env_vars: [{ key: 'ANTHROPIC_API_KEY', masked_value: 'sk…alue' }],
      },
    })
    const wrapper = mountForm()
    await flushPromises()
    await switchTab(wrapper, 'Environment')
    expect(wrapper.text()).toContain('ANTHROPIC_API_KEY')
    expect(wrapper.text()).toContain('sk…alue')
    expect(wrapper.text()).not.toContain('No guild credentials configured')
  })

  it('shows an empty-state message when the guild has no credentials', async () => {
    mockFetch({})
    const wrapper = mountForm()
    await flushPromises()
    await switchTab(wrapper, 'Environment')
    expect(wrapper.text()).toContain('No guild credentials configured')
  })

  it('excludes an unchecked credential from the spawn request', async () => {
    const spawnSpy = vi.fn((body: unknown) => ({ worker_id: 'w1', body }))
    mockFetch(
      {
        credentials: {
          guild_env_vars: [{ key: 'ANTHROPIC_API_KEY', masked_value: 'sk…alue' }],
          claude_credentials: { saved: false, updated_at: null },
        },
      },
      spawnSpy,
    )
    const wrapper = mountForm()
    await flushPromises()
    await selectFirstRepo(wrapper)
    await switchTab(wrapper, 'Environment')

    const credCheckbox = wrapper.find('.spawn-cred-row input[type="checkbox"]')
    expect(credCheckbox.exists()).toBe(true)
    await credCheckbox.setValue(false)

    await wrapper.find('.spawn-launch-btn').trigger('click')
    await flushPromises()

    expect(spawnSpy).toHaveBeenCalledOnce()
    const sentBody = spawnSpy.mock.calls[0][0] as { exclude_env_keys?: string[] }
    expect(sentBody.exclude_env_keys).toEqual(['ANTHROPIC_API_KEY'])
  })

  it('keeps a user env var that overrides a guild credential editable and sends it', async () => {
    const spawnSpy = vi.fn((body: unknown) => ({ worker_id: 'w1', body }))
    mockFetch(
      {
        credentials: {
          guild_env_vars: [{ key: 'ANTHROPIC_API_KEY', masked_value: 'sk…alue' }],
          claude_credentials: { saved: false, updated_at: null },
        },
        settings: { envVars: [{ key: 'ANTHROPIC_API_KEY', value: 'mine' }] },
      },
      spawnSpy,
    )
    const wrapper = mountForm()
    await flushPromises()
    await selectFirstRepo(wrapper)
    await switchTab(wrapper, 'Environment')

    // The saved pair is still an editable row, not swallowed by the guild list.
    const keyInputs = wrapper.findAll('.spawn-env-key')
    expect(keyInputs).toHaveLength(1)
    expect((keyInputs[0].element as HTMLInputElement).value).toBe('ANTHROPIC_API_KEY')
    expect(wrapper.text()).toContain('overrides guild')
    expect(wrapper.text()).toContain('overridden below')

    await wrapper.find('.spawn-launch-btn').trigger('click')
    await flushPromises()

    const sentBody = spawnSpy.mock.calls[0][0] as {
      env_vars?: Record<string, string>
      exclude_env_keys?: string[]
    }
    expect(sentBody.env_vars).toEqual({ ANTHROPIC_API_KEY: 'mine' })
    // An overridden key is replaced, not excluded.
    expect(sentBody.exclude_env_keys).toBeUndefined()
  })

  it('shows the guild per-tool env vars alongside the user overrides', async () => {
    mockFetch({
      credentials: {
        guild_env_vars: [],
        guild_tool_env_vars: { claude: [{ key: 'GUILD_TOOL_KEY', masked_value: 'gu…ey' }] },
        claude_credentials: { saved: false, updated_at: null },
      },
    })
    const wrapper = mountForm()
    await flushPromises()
    await switchTab(wrapper, 'Environment')
    expect(wrapper.text()).toContain('GUILD_TOOL_KEY')
    expect(wrapper.text()).toContain('gu…ey')
  })

  it('rejects an invalid env var key before spawning', async () => {
    const spawnSpy = vi.fn((body: unknown) => ({ worker_id: 'w1', body }))
    mockFetch({}, spawnSpy)
    const wrapper = mountForm()
    await flushPromises()
    await selectFirstRepo(wrapper)
    await switchTab(wrapper, 'Environment')

    await wrapper.find('.spawn-env-add').trigger('click')
    const keyInput = wrapper.find('.spawn-env-key')
    await keyInput.setValue('not a valid key')

    await wrapper.find('.spawn-launch-btn').trigger('click')
    await flushPromises()

    expect(spawnSpy).not.toHaveBeenCalled()
    expect(wrapper.find('.spawn-error').text()).toContain('Invalid env var key')
  })
})

describe('SpawnWorkerForm guild defaults and saved settings', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  const TWO_REPOS = [{ full_name: 'org/alpha' }, { full_name: 'org/beta' }]

  function checkedRepos(wrapper: ReturnType<typeof mountForm>): string[] {
    return wrapper
      .findAll('.spawn-repo-list .spawn-repo-indent')
      .filter((row) => (row.find('input').element as HTMLInputElement).checked)
      .map((row) => row.find('.spawn-repo-name').text())
  }

  it('prefills from the guild defaults and labels them', async () => {
    mockFetch({ defaults: { repos: ['org/alpha'], tools: ['pi'], agent_count: 2 } })
    const wrapper = mountForm(TWO_REPOS)
    await flushPromises()

    expect(checkedRepos(wrapper)).toEqual(['alpha'])
    expect(wrapper.text()).toContain('Showing the guild defaults.')
    expect(wrapper.text()).toContain('Guild default: org/alpha')
    // The guild's repos are marked as such even when the selection diverges.
    expect(wrapper.find('.spawn-repo-indent .spawn-badge').text()).toBe('guild')

    await switchTab(wrapper, 'Tools')
    expect(wrapper.text()).toContain('Guild default: pi')
    expect(wrapper.text()).toContain('Guild default: 2')
  })

  it('prefers the last-used settings and can switch between the two', async () => {
    mockFetch({
      defaults: { repos: ['org/alpha'], tools: ['pi'], agent_count: 2 },
      settings: { repos: ['org/beta'], tools: ['claude'] },
    })
    const wrapper = mountForm(TWO_REPOS)
    await flushPromises()

    expect(checkedRepos(wrapper)).toEqual(['beta'])
    expect(wrapper.text()).toContain('Showing your settings from your last launch.')

    const useGuild = wrapper
      .findAll('.spawn-defaults-reset')
      .find((b) => b.text() === 'Use guild defaults')
    await useGuild!.trigger('click')
    expect(checkedRepos(wrapper)).toEqual(['alpha'])
    expect(wrapper.text()).toContain('Showing the guild defaults.')

    const useLast = wrapper
      .findAll('.spawn-defaults-reset')
      .find((b) => b.text() === 'Use last launch')
    await useLast!.trigger('click')
    expect(checkedRepos(wrapper)).toEqual(['beta'])
  })

  it("keeps the user's own env vars when resetting to the guild defaults", async () => {
    // "Use guild defaults" is about the launch shape (repos/tools/agent count).
    // The guild's credentials are a separate, masked list — so resetting must
    // not silently drop env vars the user typed and saved (#1240).
    mockFetch({
      defaults: { repos: ['org/alpha'], tools: ['pi'], agent_count: 2 },
      settings: { repos: ['org/beta'], envVars: [{ key: 'MY_TOKEN', value: 'mine' }] },
    })
    const wrapper = mountForm(TWO_REPOS)
    await flushPromises()

    const useGuild = wrapper
      .findAll('.spawn-defaults-reset')
      .find((b) => b.text() === 'Use guild defaults')
    await useGuild!.trigger('click')

    expect(checkedRepos(wrapper)).toEqual(['alpha'])
    expect(wrapper.text()).toContain('Showing the guild defaults.')

    await switchTab(wrapper, 'Environment')
    const keys = wrapper
      .findAll('.spawn-env-row input')
      .map((i) => (i.element as HTMLInputElement).value)
    expect(keys).toContain('MY_TOKEN')
    expect(keys).toContain('mine')
  })

  it('marks the form as customised once a repo is toggled by hand', async () => {
    mockFetch({ defaults: { repos: ['org/alpha'], tools: [], agent_count: null } })
    const wrapper = mountForm(TWO_REPOS)
    await flushPromises()

    const rows = wrapper.findAll('.spawn-repo-list .spawn-repo-indent input')
    await rows[1].setValue(true)

    expect(checkedRepos(wrapper)).toEqual(['alpha', 'beta'])
    expect(wrapper.text()).toContain('Showing your edits for this launch.')
    expect(wrapper.text()).toContain('Use guild defaults')
  })
})
