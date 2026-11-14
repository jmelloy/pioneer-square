import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { enableAutoUnmount, mount } from '@vue/test-utils'
import ProjectsView from '../ProjectsView.vue'
import { useGitHubStore } from '../../stores/github'
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
})
