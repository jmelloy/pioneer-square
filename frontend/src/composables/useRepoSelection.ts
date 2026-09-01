/**
 * The repo/org/tool checkbox behaviour shared by every spawn-settings form
 * (Launch, User Preferences, Guild Settings). It was copy-pasted into all three
 * byte-identical modulo the ref name (issue #1240).
 *
 * `selected` and `tools` are the caller's own refs — this only owns the toggle
 * logic, not the state, so a form can keep validating/persisting them as it likes.
 * `onChange` fires on every user-driven toggle (the Launch form uses it to flip
 * its "showing your edits" affordance).
 */
import { computed, type Ref } from 'vue'
import { groupAndSortRepos } from '../utils/repoGroups'
import { useGitHubStore } from '../stores/github'

export function useRepoSelection(
  selected: Ref<string[]>,
  tools: Ref<string[]>,
  onChange: () => void = () => {},
) {
  const ghStore = useGitHubStore()
  const groupedRepos = computed(() => groupAndSortRepos(ghStore.repos))

  const orgRepos = (owner: string) => groupedRepos.value.find((g) => g.owner === owner)?.repos ?? []

  function toggleRepo(fullName: string) {
    onChange()
    const idx = selected.value.indexOf(fullName)
    if (idx >= 0) selected.value.splice(idx, 1)
    else selected.value.push(fullName)
  }

  function orgAllSelected(owner: string): boolean {
    const rs = orgRepos(owner)
    return rs.length > 0 && rs.every((r) => selected.value.includes(r.full_name))
  }

  function orgSomeSelected(owner: string): boolean {
    return orgRepos(owner).some((r) => selected.value.includes(r.full_name))
  }

  function toggleOrg(owner: string) {
    onChange()
    const rs = orgRepos(owner)
    if (orgAllSelected(owner)) {
      const names = new Set(rs.map((r) => r.full_name))
      selected.value = selected.value.filter((n) => !names.has(n))
    } else {
      for (const repo of rs) {
        if (!selected.value.includes(repo.full_name)) selected.value.push(repo.full_name)
      }
    }
  }

  /** An org checkbox is "indeterminate" (a dash, not a tick) when only some of
   *  its repos are picked. There is no HTML attribute for it — it has to be set
   *  on the element, so the template binds this as a :ref. */
  function setOrgCheckboxRef(el: HTMLInputElement | null, owner: string) {
    if (el) el.indeterminate = orgSomeSelected(owner) && !orgAllSelected(owner)
  }

  function toggleTool(tool: string) {
    onChange()
    const idx = tools.value.indexOf(tool)
    if (idx >= 0) tools.value.splice(idx, 1)
    else tools.value.push(tool)
  }

  return {
    groupedRepos,
    toggleRepo,
    orgAllSelected,
    toggleOrg,
    setOrgCheckboxRef,
    toggleTool,
  }
}
