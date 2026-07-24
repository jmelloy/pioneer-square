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
}

function mockFetch(routes: Routes, onSpawn?: (body: unknown) => unknown) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
    const url = String(input)
    if (url.includes('/spawn-credentials')) {
      return Promise.resolve(jsonResponse(routes.credentials ?? { guild_env_vars: [], claude_credentials: { saved: false, updated_at: null } }))
    }
    if (url.includes('/spawn-worker')) {
      const body = init?.body ? JSON.parse(init.body as string) : {}
      return Promise.resolve(jsonResponse(onSpawn ? onSpawn(body) : { worker_id: 'w1' }))
    }
    if (url.includes('/spawn-settings')) return Promise.resolve(jsonResponse({}))
    if (url.includes('/spawn-defaults')) return Promise.resolve(jsonResponse({}))
    return Promise.resolve(jsonResponse({}))
  })
}

function mountForm() {
  const auth = useAuthStore()
  auth.user = { id: 'u1', login: 'me' }
  auth.loginToken = 'tok'
  const guild = useGuildStore()
  guild.currentGuild = { id: 'g1', name: 'Guild', primary_repo: null }
  const gh = useGitHubStore()
  gh.repos = [{ full_name: 'org/repo' }]
  return mount(SpawnWorkerForm)
}

async function selectFirstRepo(wrapper: ReturnType<typeof mountForm>) {
  const repoCheckboxes = wrapper.findAll('.spawn-repo-list input[type="checkbox"]')
  // Index 0 is the "select all" org checkbox; index 1 is the repo itself.
  await repoCheckboxes[1].setValue(true)
}

describe('SpawnWorkerForm credentials', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows masked guild env vars and claude credential status', async () => {
    mockFetch({
      credentials: {
        guild_env_vars: [{ key: 'ANTHROPIC_API_KEY', masked_value: 'sk…alue' }],
        claude_credentials: { saved: true, updated_at: '2026-07-24T00:00:00Z' },
      },
    })
    const wrapper = mountForm()
    await flushPromises()
    expect(wrapper.text()).toContain('ANTHROPIC_API_KEY')
    expect(wrapper.text()).toContain('sk…alue')
    expect(wrapper.text()).toContain('configured')
    expect(wrapper.text()).not.toContain('No guild credentials configured')
  })

  it('shows an empty-state message when the guild has no credentials', async () => {
    mockFetch({})
    const wrapper = mountForm()
    await flushPromises()
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

    const credCheckbox = wrapper.find('.spawn-cred-row input[type="checkbox"]')
    expect(credCheckbox.exists()).toBe(true)
    await credCheckbox.setValue(false)

    await wrapper.find('.spawn-launch-btn').trigger('click')
    await flushPromises()

    expect(spawnSpy).toHaveBeenCalledOnce()
    const sentBody = spawnSpy.mock.calls[0][0] as { exclude_env_keys?: string[] }
    expect(sentBody.exclude_env_keys).toEqual(['ANTHROPIC_API_KEY'])
  })

  it('rejects an invalid env var key before spawning', async () => {
    const spawnSpy = vi.fn((body: unknown) => ({ worker_id: 'w1', body }))
    mockFetch({}, spawnSpy)
    const wrapper = mountForm()
    await flushPromises()
    await selectFirstRepo(wrapper)

    await wrapper.find('.spawn-env-add').trigger('click')
    const keyInput = wrapper.find('.spawn-env-key')
    await keyInput.setValue('not a valid key')

    await wrapper.find('.spawn-launch-btn').trigger('click')
    await flushPromises()

    expect(spawnSpy).not.toHaveBeenCalled()
    expect(wrapper.find('.spawn-error').text()).toContain('Invalid env var key')
  })
})
