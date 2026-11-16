import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
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
function render() {
  return mount(ProjectsView, {
    global: {
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
    const store = useGitHubStore()
    store.token = 'test-token'
    store.selectedRepos = ['team/factory', 'team/engine']
    store.issues = [
      issue(1),
      issue(2, 'team/engine', 'closed'),
      issue(3),
      issue(4, 'other/private'),
    ]
    vi.spyOn(store, 'fetchIssues').mockResolvedValue([])
  })
  afterEach(() => vi.restoreAllMocks())

  it('defaults to open issues in configured repositories, newest first', () => {
    const wrapper = render()
    expect(wrapper.findAll('tbody tr').map((row) => row.find('td').text())).toEqual(['3', '1'])
    expect(wrapper.find('.viewer-stub').text()).toBe('factory #3')
    expect(useGitHubStore().fetchIssues).toHaveBeenCalledWith(['team/factory', 'team/engine'])
  })

  it('filters by project, state, and search and clears stale selection', async () => {
    const wrapper = render()
    await wrapper.findAll('.issue-status-filters button')[0]!.trigger('click')
    expect(wrapper.findAll('tbody tr')).toHaveLength(3)
    await wrapper.findAll('.issue-select')[1]!.trigger('click')
    expect(wrapper.find('.viewer-stub').text()).toBe('engine #2')
    await wrapper.findAll('.project-list .project-choice')[1]!.trigger('click')
    expect(wrapper.findAll('tbody tr')).toHaveLength(2)
    expect(wrapper.find('.viewer-stub').text()).toBe('factory #3')
    await wrapper.find('[aria-label="Search issues"]').setValue('Issue 1')
    expect(wrapper.findAll('tbody tr')).toHaveLength(1)
    await wrapper.findAll('.issue-status-filters button')[2]!.trigger('click')
    expect(wrapper.findAll('tbody tr')).toHaveLength(0)
    expect(wrapper.find('.viewer-stub').exists()).toBe(false)
  })

  it('paginates and resets the page when filters change', async () => {
    useGitHubStore().issues = Array.from({ length: 23 }, (_, i) => issue(i + 1))
    const wrapper = render()
    expect(wrapper.findAll('tbody tr')).toHaveLength(20)
    await wrapper.find('[aria-label="Next page"]').trigger('click')
    expect(wrapper.findAll('tbody tr')).toHaveLength(3)
    expect(wrapper.find('.viewer-stub').text()).toBe('factory #3')
    await wrapper.find('[aria-label="Search issues"]').setValue('Issue 23')
    expect(wrapper.findAll('tbody tr')).toHaveLength(1)
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
    expect(wrapper.find('[role="alert"]').text()).toBe('GitHub unavailable')
  })

  describe('repository list editor', () => {
    function mockRepos(names = ['team/factory', 'team/engine', 'team/docs', 'acme/site']) {
      return vi.spyOn(useGitHubStore(), 'fetchRepos').mockImplementation(async () => {
        const store = useGitHubStore()
        store.repos = names.map((full_name) => ({ full_name }) as never)
        return store.repos
      })
    }
    async function openEditor() {
      const wrapper = render()
      await wrapper.find('.projects-heading button:last-child').trigger('click')
      await flushPromises()
      return wrapper
    }
    const selected = (w: ReturnType<typeof render>) =>
      w.findAll('[aria-label="Selected repositories"] li span').map((s) => s.text())

    it('searches, adds, removes, and saves the browsing list', async () => {
      mockRepos()
      const wrapper = await openEditor()
      expect(selected(wrapper)).toEqual(['team/factory', 'team/engine'])
      await wrapper.find('[aria-label="Search GitHub repositories"]').setValue('acme')
      expect(wrapper.findAll('[aria-label="Available repositories"] li span')).toHaveLength(1)
      await wrapper.find('[aria-label="Add acme/site"]').trigger('click')
      await wrapper.find('[aria-label="Remove team/engine"]').trigger('click')
      expect(selected(wrapper)).toEqual(['team/factory', 'acme/site'])
      const save = wrapper.findAll('.repo-editor footer button').find((b) => b.text() === 'Save')!
      await save.trigger('click')
      const store = useGitHubStore()
      expect(store.selectedRepos).toEqual(['team/factory', 'acme/site'])
      expect(store.fetchIssues).toHaveBeenLastCalledWith(['team/factory', 'acme/site'])
      expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    })

    it('adds the first match with Enter and discards changes on Escape', async () => {
      mockRepos()
      const wrapper = await openEditor()
      const input = wrapper.find('[aria-label="Search GitHub repositories"]')
      await input.setValue('docs')
      await input.trigger('keydown', { key: 'Enter' })
      expect(selected(wrapper)).toContain('team/docs')
      await wrapper.find('[role="dialog"]').trigger('keydown', { key: 'Escape' })
      expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
      expect(useGitHubStore().selectedRepos).toEqual(['team/factory', 'team/engine'])
    })

    it('shows load errors with retry without clobbering the issues error', async () => {
      const store = useGitHubStore()
      store.error = 'issues failed'
      const spy = vi.spyOn(store, 'fetchRepos').mockImplementation(async () => {
        store.error = 'GitHub error 500'
        return []
      })
      const wrapper = await openEditor()
      expect(wrapper.find('.repo-editor [role="alert"]').text()).toContain('GitHub error 500')
      expect(store.error).toBe('issues failed')
      spy.mockImplementation(async () => {
        store.repos = [{ full_name: 'team/docs' } as never]
        return store.repos
      })
      await wrapper.find('.repo-editor [role="alert"] button').trigger('click')
      await flushPromises()
      expect(wrapper.find('[aria-label="Add team/docs"]').exists()).toBe(true)
    })

    it('offers an actionable empty state', async () => {
      mockRepos()
      useGitHubStore().selectedRepos = []
      const wrapper = render()
      await wrapper.find('.project-list .workspace-empty button').trigger('click')
      await flushPromises()
      expect(wrapper.find('[role="dialog"]').text()).toContain('Nothing selected yet')
    })

    it('explains the primary-repo override', async () => {
      mockRepos()
      useGuildStore().currentGuild = { primary_repo: 'team/main' } as never
      const wrapper = render()
      expect(wrapper.find('.project-main .primary-override').text()).toContain('team/main')
      expect(useGitHubStore().fetchIssues).toHaveBeenCalledWith(['team/main'])
    })
  })
})
