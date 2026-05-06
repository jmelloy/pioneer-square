import type { GitHubRepo } from '../types'

export interface RepoGroup {
  owner: string
  repos: GitHubRepo[]
}

export function groupAndSortRepos(repos: GitHubRepo[]): RepoGroup[] {
  const sorted = [...repos].sort((a, b) => a.full_name.localeCompare(b.full_name))
  const groupMap = new Map<string, GitHubRepo[]>()
  for (const repo of sorted) {
    const owner = repo.full_name.split('/')[0]
    if (!groupMap.has(owner)) groupMap.set(owner, [])
    groupMap.get(owner)!.push(repo)
  }
  return Array.from(groupMap.entries()).map(([owner, repos]) => ({ owner, repos }))
}
