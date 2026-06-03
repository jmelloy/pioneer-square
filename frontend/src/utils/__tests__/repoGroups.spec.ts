import { describe, expect, it } from 'vitest'
import { groupAndSortRepos } from '../repoGroups'
import type { GitHubRepo } from '../../types'

function repo(full_name: string): GitHubRepo {
  return { full_name }
}

describe('groupAndSortRepos', () => {
  it('returns empty array for empty input', () => {
    expect(groupAndSortRepos([])).toEqual([])
  })

  it('sorts repos alphabetically by full_name within each group', () => {
    const repos = [repo('acme/zebra'), repo('acme/alpha'), repo('acme/middle')]
    const groups = groupAndSortRepos(repos)
    expect(groups).toHaveLength(1)
    expect(groups[0].repos.map((r) => r.full_name)).toEqual([
      'acme/alpha',
      'acme/middle',
      'acme/zebra',
    ])
  })

  it('groups repos by owner', () => {
    const repos = [repo('bob/repo1'), repo('alice/repoA'), repo('alice/repoB'), repo('bob/repo2')]
    const groups = groupAndSortRepos(repos)
    expect(groups.map((g) => g.owner)).toEqual(['alice', 'bob'])
    expect(groups[0].repos.map((r) => r.full_name)).toEqual(['alice/repoA', 'alice/repoB'])
    expect(groups[1].repos.map((r) => r.full_name)).toEqual(['bob/repo1', 'bob/repo2'])
  })

  it('orders groups alphabetically by owner', () => {
    const repos = [repo('zebra-org/a'), repo('alpha-org/b'), repo('middle-org/c')]
    const groups = groupAndSortRepos(repos)
    expect(groups.map((g) => g.owner)).toEqual(['alpha-org', 'middle-org', 'zebra-org'])
  })

  it('does not mutate the input array', () => {
    const repos = [repo('b/repo'), repo('a/repo')]
    const original = [...repos]
    groupAndSortRepos(repos)
    expect(repos).toEqual(original)
  })

  it('handles a single repo', () => {
    const groups = groupAndSortRepos([repo('owner/name')])
    expect(groups).toHaveLength(1)
    expect(groups[0].owner).toBe('owner')
    expect(groups[0].repos).toHaveLength(1)
  })
})
