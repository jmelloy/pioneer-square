import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import SpawnWorkerForm from '../SpawnWorkerForm.vue'
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
  spawnSettings?: unknown
  spawnDefaults?: unknown
}

// Route the two GETs the form issues on mount by URL, so the test does not
// depend on call ordering.
function mockFetch(routes: Routes) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/spawn-settings')) {
      return Promise.resolve(jsonResponse(routes.spawnSettings ?? {}))
    }
    if (url.includes('/spawn-defaults')) {
      return Promise.resolve(jsonResponse(routes.spawnDefaults ?? {}))
    }
    return Promise.resolve(jsonResponse({}))
  })
}

function primeStores() {
  const guild = useGuildStore()
  guild.currentGuild = { id: 'g1', name: 'Test Guild' }
  const gh = useGitHubStore()
  gh.token = 'gh-tok'
  // Pre-set repos so the form skips its GH-API fetch on mount.
  gh.repos = [{ full_name: 'acme/widgets' }, { full_name: 'acme/gears' }] as never
}

async function mountForm(routes: Routes) {
  mockFetch(routes)
  primeStores()
  const wrapper = mount(SpawnWorkerForm)
  await flushPromises()
  return wrapper
}

describe('SpawnWorkerForm guild-default pre-fill', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('pre-fills repos, tools, and agent count from guild spawn defaults', async () => {
    const wrapper = await mountForm({
      spawnSettings: {},
      spawnDefaults: { repos: ['acme/widgets'], tools: ['claude', 'codex'], agent_count: 2 },
    })

    // The guild default repo is selected and its row is rendered as selected.
    const widgetsRow = wrapper.findAll('.spawn-repo-row').find((r) => r.text().includes('widgets'))
    expect(widgetsRow?.classes()).toContain('selected')

    // Tools from the guild default are selected.
    const claudeRow = wrapper.findAll('.spawn-repo-row').find((r) => r.text() === 'claude')
    const codexRow = wrapper.findAll('.spawn-repo-row').find((r) => r.text() === 'codex')
    expect(claudeRow?.classes()).toContain('selected')
    expect(codexRow?.classes()).toContain('selected')

    // Agent count is pre-filled.
    const agentInput = wrapper.find('input[type="number"]').element as HTMLInputElement
    expect(agentInput.value).toBe('2')

    // The explanatory note is shown and Launch is enabled.
    expect(wrapper.find('.spawn-prefill-note').exists()).toBe(true)
    const launchBtn = wrapper.find('.spawn-launch-btn').element as HTMLButtonElement
    expect(launchBtn.disabled).toBe(false)
  })

  it('drops guild-default repos that are not in the available list', async () => {
    const wrapper = await mountForm({
      spawnSettings: {},
      spawnDefaults: { repos: ['acme/widgets', 'ghost/removed'], tools: [] },
    })

    const selectedNames = wrapper.findAll('.spawn-repo-row.selected').map((r) => r.text())
    expect(selectedNames.some((n) => n.includes('widgets'))).toBe(true)
    expect(selectedNames.some((n) => n.includes('removed'))).toBe(false)
  })

  it('prefers the user’s personal saved settings over guild defaults', async () => {
    const fetchSpy = await mountForm({
      spawnSettings: { repos: ['acme/gears'], tools: ['pi'] },
      spawnDefaults: { repos: ['acme/widgets'], tools: ['claude'] },
    }).then((w) => w)

    const gearsRow = fetchSpy.findAll('.spawn-repo-row').find((r) => r.text().includes('gears'))
    const widgetsRow = fetchSpy.findAll('.spawn-repo-row').find((r) => r.text().includes('widgets'))
    expect(gearsRow?.classes()).toContain('selected')
    expect(widgetsRow?.classes()).not.toContain('selected')
    // Guild defaults did not apply, so no pre-fill note.
    expect(fetchSpy.find('.spawn-prefill-note').exists()).toBe(false)
  })

  it('dismisses the pre-fill note once the user edits the repo selection', async () => {
    const wrapper = await mountForm({
      spawnSettings: {},
      spawnDefaults: { repos: ['acme/widgets'], tools: [] },
    })
    expect(wrapper.find('.spawn-prefill-note').exists()).toBe(true)

    // Toggle a different repo → the note should disappear.
    const gearsCheckbox = wrapper
      .findAll('.spawn-repo-row')
      .find((r) => r.text().includes('gears'))!
      .find('input[type="checkbox"]')
    await gearsCheckbox.setValue(true)
    await flushPromises()
    expect(wrapper.find('.spawn-prefill-note').exists()).toBe(false)
  })
})
