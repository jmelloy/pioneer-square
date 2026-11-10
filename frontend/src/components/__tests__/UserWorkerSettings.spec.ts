import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import UserWorkerSettings from '../UserWorkerSettings.vue'
import { useAuthStore } from '../../stores/auth'

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response
}

interface Routes {
  spawnSettings?: unknown
}

function mockFetch(routes: Routes, onPut?: (body: unknown) => unknown) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
    const url = String(input)
    if (url.includes('/spawn-settings')) {
      if (init?.method === 'PUT') {
        const body = init.body ? JSON.parse(init.body as string) : {}
        return Promise.resolve(jsonResponse(onPut ? onPut(body) : body))
      }
      return Promise.resolve(jsonResponse(routes.spawnSettings ?? {}))
    }
    if (url.includes('/api/models')) return Promise.resolve(jsonResponse([]))
    return Promise.resolve(jsonResponse({}))
  })
}

function mountAs(userId: string, props: { guildId?: string } = { guildId: 'g1' }) {
  const auth = useAuthStore()
  auth.user = { id: userId, login: 'me' }
  auth.loginToken = 'tok'
  return mount(UserWorkerSettings, { props })
}

describe('UserWorkerSettings', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('prompts to open a guild when none is active', async () => {
    mockFetch({})
    const wrapper = mountAs('u-1', {})
    await flushPromises()
    expect(wrapper.text()).toContain('Open a guild')
    expect(wrapper.find('.uws-save-btn').exists()).toBe(false)
  })

  it('loads and shows the current user spawn settings for the guild', async () => {
    mockFetch({
      spawnSettings: {
        repos: ['acme/api'],
        tools: ['claude'],
        envVars: [{ key: 'FOO', value: 'bar' }],
        toolDefaults: { pi: { provider: 'anthropic', model: 'claude-sonnet-4-6' } },
        toolEnvVars: {},
      },
    })
    const wrapper = mountAs('u-1')
    await flushPromises()
    expect(wrapper.find('.uws-save-btn').exists()).toBe(true)
    const keyInput = wrapper.find('.env-var-key').element as HTMLInputElement
    expect(keyInput.value).toBe('FOO')
  })

  it('saves the edited settings via PUT', async () => {
    const put = vi.fn((body: unknown) => body)
    mockFetch(
      {
        spawnSettings: { repos: [], tools: [], envVars: [], toolDefaults: {}, toolEnvVars: {} },
      },
      put,
    )
    const wrapper = mountAs('u-1')
    await flushPromises()
    await wrapper.find('.uws-save-btn').trigger('click')
    await flushPromises()
    expect(put).toHaveBeenCalledOnce()
    expect(put).toHaveBeenCalledWith(expect.objectContaining({ repos: [], tools: [], envVars: [] }))
    expect(wrapper.find('.save-status-saved').exists()).toBe(true)
  })
})

describe('UserWorkerSettings repository setup', () => {
  beforeEach(() => setActivePinia(createPinia()))
  afterEach(() => vi.restoreAllMocks())

  function setupFetch(initial: unknown, calls: { method: string; body?: unknown }[]) {
    return vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const url = String(input)
      if (url.includes('/repos/acme/api/setup')) {
        const method = init?.method ?? 'GET'
        const body = init?.body ? JSON.parse(init.body as string) : undefined
        calls.push({ method, body })
        if (method === 'PUT')
          return Promise.resolve(
            jsonResponse({
              setupCommands: body.setup_commands,
              timeoutSeconds: body.timeout_seconds,
            }),
          )
        if (method === 'DELETE') return Promise.resolve(jsonResponse({ status: 'deleted' }))
        return Promise.resolve(jsonResponse(initial))
      }
      if (url.includes('/spawn-settings'))
        return Promise.resolve(jsonResponse({ repos: ['acme/api'] }))
      return Promise.resolve(jsonResponse([]))
    })
  }

  it('loads, saves and clears per-repo setup commands', async () => {
    const calls: { method: string; body?: unknown }[] = []
    setupFetch({ setupCommands: ['npm ci'], timeoutSeconds: 90 }, calls)
    const wrapper = mountAs('u-1')
    await flushPromises()

    await wrapper.find('[data-testid="setup-repo"]').setValue('acme/api')
    await flushPromises()
    const ta = wrapper.find('[data-testid="setup-commands"]')
    expect((ta.element as HTMLTextAreaElement).value).toBe('npm ci')
    expect((wrapper.find('[data-testid="setup-timeout"]').element as HTMLInputElement).value).toBe(
      '90',
    )
    expect(wrapper.find('[data-testid="setup-current"]').text()).toContain('1 command')

    await ta.setValue('npm ci\nnpm run build\n')
    await wrapper.find('[data-testid="setup-save"]').trigger('click')
    await flushPromises()
    const put = calls.find((c) => c.method === 'PUT')
    expect(put?.body).toMatchObject({ timeout_seconds: 90 })
    expect((put?.body as { setup_commands: string[] }).setup_commands).toContain('npm run build')

    await wrapper.find('[data-testid="setup-clear"]').trigger('click')
    await flushPromises()
    expect(calls.some((c) => c.method === 'DELETE')).toBe(true)
    expect(
      (wrapper.find('[data-testid="setup-commands"]').element as HTMLTextAreaElement).value,
    ).toBe('')
  })
})
