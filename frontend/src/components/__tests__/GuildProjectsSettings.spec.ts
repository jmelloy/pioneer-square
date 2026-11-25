import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import GuildProjectsSettings from '../GuildProjectsSettings.vue'
import { useGitHubStore } from '../../stores/github'
import { useGuildStore } from '../../stores/guild'

describe('GuildProjectsSettings', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.setItem('gh_token', 'tok')
    useGuildStore().currentGuild = { id: 'g1', browse_repos: ['team/factory', 'team/engine'] }
    const store = useGitHubStore()
    store.token = 'tok'
    vi.spyOn(store, 'fetchRepos').mockImplementation(async () => {
      store.repos = ['team/factory', 'team/engine', 'acme/site'].map(
        (full_name) => ({ full_name }) as never,
      )
      return store.repos
    })
  })

  it('edits and saves the repository list', async () => {
    const fetchMock = vi.fn(async (_u: string, init: RequestInit) =>
      Response.json({ browse_repos: JSON.parse(init.body as string).repos }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const w = mount(GuildProjectsSettings)
    await flushPromises()
    await w.find('[aria-label="Add acme/site"]').trigger('click')
    await w.find('[aria-label="Remove team/engine"]').trigger('click')
    await w
      .findAll('button')
      .find((b) => b.text() === 'Save repositories')!
      .trigger('click')
    await flushPromises()
    expect(fetchMock.mock.calls[0]![0]).toContain('/api/guilds/g1/browse-repos')
    expect(useGitHubStore().selectedRepos).toEqual(['team/factory', 'acme/site'])
  })

  it('shows a save error and keeps the stored list', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Response.json({ detail: 'requires owner, member' }, { status: 403 })),
    )
    const w = mount(GuildProjectsSettings)
    await flushPromises()
    await w.find('[aria-label="Remove team/engine"]').trigger('click')
    await w
      .findAll('button')
      .find((b) => b.text() === 'Save repositories')!
      .trigger('click')
    await flushPromises()
    expect(w.find('[role="alert"]').text()).toContain('owner, member')
    expect(useGitHubStore().selectedRepos).toEqual(['team/factory', 'team/engine'])
  })

  it('fetches user, org and repo projects and links one', async () => {
    const urls: string[] = []
    const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
      const url = String(input)
      urls.push(url)
      if (url.endsWith('/user')) return Response.json({ login: 'me' })
      if (url.endsWith('/graphql')) return Response.json({ data: {} })
      if (url.includes('/orgs/team/projects'))
        return Response.json([{ id: 7, name: 'Roadmap', html_url: 'https://x/7', state: 'open' }])
      if (init?.method === 'PUT')
        return Response.json({ browse_projects: JSON.parse(init.body as string).projects })
      return Response.json([])
    })
    vi.stubGlobal('fetch', fetchMock)
    const w = mount(GuildProjectsSettings)
    await flushPromises()
    await w
      .findAll('button')
      .find((b) => b.text() === 'Find GitHub projects')!
      .trigger('click')
    await flushPromises()
    expect(urls.some((u) => u.includes('/users/me/projects'))).toBe(true)
    expect(urls.some((u) => u.includes('/orgs/team/projects'))).toBe(true)
    expect(urls.some((u) => u.includes('/repos/team/factory/projects'))).toBe(true)
    await w.find('[aria-label="Link Roadmap"]').trigger('click')
    await w
      .findAll('button')
      .find((b) => b.text() === 'Save projects')!
      .trigger('click')
    await flushPromises()
    expect(useGitHubStore().linkedProjects.map((p) => p.name)).toEqual(['Roadmap'])
  })
})
