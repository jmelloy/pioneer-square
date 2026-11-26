import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import ProjectsView from '../ProjectsView.vue'
import { useGitHubStore } from '../../stores/github'
import { useGuildStore } from '../../stores/guild'
import type { GitHubIssue } from '../../types'

enableAutoUnmount(afterEach)

function issue(id: number, repo = 'team/factory', state = 'open'): GitHubIssue {
  return {
    id,
    number: id,
    title: `Issue ${id}`,
    repo,
    state,
    labels: [],
    created_at: new Date(2026, 0, id).toISOString(),
  }
}
let router: Router
function render() {
  return mount(ProjectsView, {
    global: {
      plugins: [router],
      stubs: {
        IssueViewer: {
          props: ['issueNumber', 'repo'],
          template: '<div class="viewer-stub">{{ repo }} #{{ issueNumber }}</div>',
        },
      },
    },
  })
}

describe('ProjectsView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/:pathMatch(.*)*', component: { template: '<div />' } }],
    })
    const store = useGitHubStore()
    store.token = 'test-token'
    useGuildStore().currentGuild = { id: 'g1', browse_repos: ['team/factory', 'team/engine'] }
    store.issues = [
      issue(1),
      issue(2, 'team/engine', 'closed'),
      issue(3),
      issue(4, 'other/private'),
    ]
    vi.spyOn(store, 'fetchIssues').mockResolvedValue([])
  })
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    localStorage.clear()
  })

  it('defaults to open issues in configured repositories, newest first', () => {
    const wrapper = render()
    expect(wrapper.findAll('.issue-row').map((row) => row.find('.issue-num').text())).toEqual([
      '3',
      '1',
    ])
    expect(wrapper.find('.viewer-stub').text()).toBe('factory #3')
    expect(useGitHubStore().fetchIssues).toHaveBeenCalledWith(['team/factory', 'team/engine'])
  })

  it('counts only open issues in project badges regardless of list filters', async () => {
    const wrapper = render()
    const counts = () => wrapper.findAll('.project-list .count').map((badge) => badge.text())
    expect(counts()).toEqual(['2', '2', '0'])
    await wrapper.findAll('.issue-status-filters button')[0]!.trigger('click')
    expect(wrapper.findAll('.issue-row')).toHaveLength(3)
    expect(counts()).toEqual(['2', '2', '0'])
    await wrapper.findAll('.project-list .project-choice')[2]!.trigger('click')
    await wrapper.findAll('.issue-status-filters button')[2]!.trigger('click')
    expect(wrapper.findAll('.issue-row')).toHaveLength(1)
    expect(counts()).toEqual(['2', '2', '0'])
    await wrapper.find('[aria-label="Search issues"]').setValue('missing')
    expect(counts()).toEqual(['2', '2', '0'])
  })

  it('filters by project, state, and search and clears stale selection', async () => {
    const wrapper = render()
    await wrapper.findAll('.issue-status-filters button')[0]!.trigger('click')
    expect(wrapper.findAll('.issue-row')).toHaveLength(3)
    await wrapper.findAll('.issue-select')[1]!.trigger('click')
    expect(wrapper.find('.viewer-stub').text()).toBe('engine #2')
    await wrapper.findAll('.project-list .project-choice')[1]!.trigger('click')
    expect(wrapper.findAll('.issue-row')).toHaveLength(2)
    expect(wrapper.find('.viewer-stub').text()).toBe('factory #3')
    await wrapper.find('[aria-label="Search issues"]').setValue('Issue 1')
    expect(wrapper.findAll('.issue-row')).toHaveLength(1)
    await wrapper.findAll('.issue-status-filters button')[2]!.trigger('click')
    expect(wrapper.findAll('.issue-row')).toHaveLength(0)
    expect(wrapper.find('.viewer-stub').exists()).toBe(false)
  })

  it('syncs project and status with the URL and follows back/forward', async () => {
    const rows = (w: ReturnType<typeof render>) =>
      w.findAll('.issue-row').map((row) => row.find('.issue-num').text())
    const path = () => router.currentRoute.value.fullPath
    await router.push('/g?project=team/engine&state=closed')
    const wrapper = render()
    expect(rows(wrapper)).toEqual(['2'])
    await wrapper.findAll('.project-list .project-choice')[0]!.trigger('click')
    await flushPromises()
    expect(path()).toBe('/g?state=closed')
    await wrapper.findAll('.issue-status-filters button')[1]!.trigger('click')
    await flushPromises()
    expect(path()).toBe('/g') // defaults are dropped from the URL
    expect(rows(wrapper)).toEqual(['3', '1'])
    router.back()
    await flushPromises()
    expect(rows(wrapper)).toEqual(['2'])
    router.back()
    await flushPromises()
    expect(path()).toBe('/g?project=team/engine&state=closed')
    router.forward()
    router.forward()
    await flushPromises()
    expect(rows(wrapper)).toEqual(['3', '1'])
  })

  describe('guild primary repo', () => {
    const rows = (w: ReturnType<typeof render>) =>
      w.findAll('.issue-row').map((row) => row.find('.issue-num').text())
    beforeEach(() => {
      useGuildStore().currentGuild!.primary_repo = 'team/engine'
      useGitHubStore().issues.push(issue(5, 'team/engine'))
    })

    it('is the default project, alongside the browsing list', () => {
      const wrapper = render()
      expect(rows(wrapper)).toEqual(['5'])
      expect(wrapper.find('.project-choice.selected').text()).toContain('engine')
      expect(useGitHubStore().fetchIssues).toHaveBeenCalledWith(['team/factory', 'team/engine'])
    })

    it('is listed even when not in the browsing list', () => {
      useGuildStore().currentGuild!.primary_repo = 'team/main'
      render()
      expect(useGitHubStore().fetchIssues).toHaveBeenCalledWith([
        'team/main',
        'team/factory',
        'team/engine',
      ])
    })

    it('does not override the URL or a manual selection', async () => {
      await router.push('/g?project=team/factory')
      const wrapper = render()
      expect(rows(wrapper)).toEqual(['3', '1'])
      await wrapper.findAll('.project-list .project-choice')[0]!.trigger('click')
      await flushPromises()
      expect(router.currentRoute.value.query).toEqual({ project: 'all' })
      expect(rows(wrapper)).toEqual(['5', '3', '1'])
      await wrapper.findAll('.project-list .project-choice')[2]!.trigger('click')
      await flushPromises()
      expect(router.currentRoute.value.query).toEqual({}) // the default needs no param
      expect(rows(wrapper)).toEqual(['5'])
    })

    it('applies when the guild loads after the page', async () => {
      useGuildStore().currentGuild = null
      const wrapper = render()
      expect(rows(wrapper)).toEqual([])
      useGuildStore().currentGuild = {
        id: 'g1',
        browse_repos: ['team/factory', 'team/engine'],
        primary_repo: 'team/engine',
      }
      await flushPromises()
      expect(rows(wrapper)).toEqual(['5'])
    })
  })

  it('ignores unknown status values in the URL', async () => {
    await router.push('/g?state=bogus')
    const wrapper = render()
    expect(wrapper.findAll('.issue-row').map((row) => row.find('.issue-num').text())).toEqual([
      '3',
      '1',
    ])
  })

  it('paginates and resets the page when filters change', async () => {
    useGitHubStore().issues = Array.from({ length: 23 }, (_, i) => issue(i + 1))
    const wrapper = render()
    expect(wrapper.findAll('.issue-row')).toHaveLength(20)
    await wrapper.find('[aria-label="Next page"]').trigger('click')
    expect(wrapper.findAll('.issue-row')).toHaveLength(3)
    expect(wrapper.find('.viewer-stub').text()).toBe('factory #3')
    await wrapper.find('[aria-label="Search issues"]').setValue('Issue 23')
    expect(wrapper.findAll('.issue-row')).toHaveLength(1)
    expect(wrapper.find('[aria-label="Previous page"]').attributes('disabled')).toBeDefined()
  })

  it('offers a list/detail back action for narrow screens', async () => {
    const wrapper = render()
    await wrapper.find('.issue-select').trigger('click')
    expect(wrapper.find('.project-main').classes()).toContain('show-detail')
    await wrapper.find('.project-back').trigger('click')
    expect(wrapper.find('.project-main').classes()).not.toContain('show-detail')
  })

  it('explains missing GitHub credentials and surfaces errors', async () => {
    const store = useGitHubStore()
    store.token = ''
    const wrapper = render()
    expect(wrapper.text()).toContain('Connect GitHub')
    store.token = 'test-token'
    store.error = 'GitHub unavailable'
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[role="alert"]').text()).toContain('GitHub unavailable')
    vi.mocked(store.fetchIssues).mockClear()
    await wrapper.find('[role="alert"] button').trigger('click')
    expect(store.fetchIssues).toHaveBeenCalledWith(['team/factory', 'team/engine'])
  })

  it('refetches immediately when the browsing scope changes, and clears stale detail', async () => {
    const store = useGitHubStore()
    const guild = useGuildStore().currentGuild!
    const wrapper = render()
    await wrapper.findAll('.issue-select')[1]!.trigger('click')
    expect(wrapper.find('.viewer-stub').text()).toBe('factory #1')
    guild.browse_repos = ['team/engine']
    await flushPromises()
    expect(store.fetchIssues).toHaveBeenLastCalledWith(['team/engine'])
    expect(wrapper.find('.viewer-stub').exists()).toBe(false)
    guild.browse_repos = []
    await flushPromises()
    expect(store.fetchIssues).toHaveBeenLastCalledWith([])
    expect(wrapper.findAll('.issue-row')).toHaveLength(0)
  })

  describe('settings handoff', () => {
    function mockPut() {
      const fetchMock = vi.fn(async (_url: string, init: RequestInit) =>
        Response.json({ browse_repos: JSON.parse(init.body as string).repos }),
      )
      vi.stubGlobal('fetch', fetchMock)
      return fetchMock
    }

    it('has no Manage in settings button in the heading', () => {
      expect(render().find('.projects-heading').text()).not.toContain('Manage in settings')
    })

    it('imports a legacy browser list only after confirmation, and only if never saved', async () => {
      localStorage.setItem('gh_repos', JSON.stringify(['team/docs']))
      const fetchMock = mockPut()
      expect(render().text()).not.toContain('Import into') // guild already has a saved list
      useGuildStore().currentGuild!.browse_repos = null
      const wrapper = render()
      expect(fetchMock).not.toHaveBeenCalled()
      await wrapper
        .findAll('button')
        .find((b) => b.text() === 'Import')!
        .trigger('click')
      await flushPromises()
      expect(useGitHubStore().selectedRepos).toEqual(['team/docs'])
      expect(localStorage.getItem('gh_repos')).toBeNull()
      expect(wrapper.text()).not.toContain('Import into')
    })

    it('lists linked GitHub projects in the sidebar', () => {
      useGuildStore().currentGuild!.browse_projects = [
        {
          id: 'p1',
          name: 'Roadmap',
          url: 'https://github.com/orgs/team/projects/1',
          owner: 'team',
          kind: 'org',
        },
      ]
      const link = render().find('a.project-choice')
      expect(link.text()).toContain('Roadmap')
      expect(link.attributes('href')).toContain('/projects/1')
    })

    it('shows a linked Projects v2 board inline from the backend cache', async () => {
      const store = useGitHubStore()
      useGuildStore().currentGuild!.browse_projects = [
        {
          id: 'PVT_1',
          name: 'Board',
          url: 'https://github.com/orgs/team/projects/2',
          owner: 'team',
          kind: 'v2',
        },
      ]
      const fetchBoard = vi
        .spyOn(store, 'fetchProjectIssues')
        .mockResolvedValue([
          { ...issue(9, 'team/engine'), status: 'In Progress' },
          issue(10, 'team/engine', 'closed'),
        ])
      const wrapper = render()
      expect(wrapper.find('a.project-choice').exists()).toBe(false) // v2 no longer links out
      const board = wrapper
        .findAll('.project-list .project-choice')
        .find((b) => b.text().includes('Board'))!
      await board.trigger('click')
      await flushPromises()
      expect(board.find('.count').text()).toBe('1')
      await wrapper.findAll('.issue-status-filters button')[0]!.trigger('click')
      expect(wrapper.findAll('.issue-row')).toHaveLength(2)
      expect(board.find('.count').text()).toBe('1')
      await wrapper.findAll('.issue-status-filters button')[1]!.trigger('click')
      expect(fetchBoard).toHaveBeenCalledWith('PVT_1', false)
      expect(router.currentRoute.value.query.board).toBe('PVT_1')
      expect(wrapper.findAll('.issue-row').map((r) => r.find('.issue-num').text())).toEqual(['9'])
      expect(wrapper.find('.board-status').text()).toBe('In Progress')
      expect(wrapper.find('.viewer-stub').text()).toBe('engine #9')
      expect(wrapper.find('.projects-heading a').attributes('href')).toContain('/projects/2')
      await wrapper.find('[aria-label="Sync"]').trigger('click')
      expect(fetchBoard).toHaveBeenLastCalledWith('PVT_1', true)
      await wrapper.findAll('.project-list .project-choice')[0]!.trigger('click')
      await flushPromises()
      expect(router.currentRoute.value.query.board).toBeUndefined()
      expect(wrapper.findAll('.issue-row')).toHaveLength(2)
    })
  })

  it('splits pull requests into their own tab and nests sub-issues under parents', async () => {
    const base = 'https://api.github.com/repos/team/factory/issues/'
    useGitHubStore().issues = [
      { ...issue(1), url: base + 1, status: { name: 'Todo' } as never },
      { ...issue(5), url: base + 5, parent_issue_url: base + 1 },
      { ...issue(6), pull_request: { merged_at: null } },
    ]
    const wrapper = render()
    const rows = () => wrapper.findAll('.issue-row').map((r) => r.find('.issue-num').text())
    expect(rows()).toEqual(['1', '5']) // child follows its parent, not newest-first
    expect(wrapper.findAll('.issue-row')[1]!.classes()).toContain('child')
    expect(wrapper.find('.board-status').text()).toBe('Todo')
    await wrapper.findAll('.project-kind-tab')[1]!.trigger('click')
    expect(rows()).toEqual(['6'])
  })
})
