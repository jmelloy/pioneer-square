<template>
  <section class="projects-workspace" aria-label="Projects and issues">
    <aside class="project-sidebar panel-bg">
      <div class="sidebar-heading">
        <h2><span class="project-gem" aria-hidden="true">◆</span> Projects</h2>
      </div>
      <input
        v-model="projectSearch"
        class="project-search"
        type="search"
        placeholder="Search projects…"
        aria-label="Search projects"
      />
      <div class="project-list">
        <button
          class="project-choice"
          :class="{ selected: !selectedRepo && !selectedBoard }"
          @click="selectRepo('')"
        >
          <span aria-hidden="true">◈</span><span>All projects</span
          ><span class="count">{{ activeIssues.length }}</span>
        </button>
        <button
          v-for="repo in filteredRepos"
          :key="repo"
          class="project-choice"
          :class="{ selected: !selectedBoard && selectedRepo === repo }"
          @click="selectRepo(repo)"
        >
          <span class="project-gem" aria-hidden="true">◆</span
          ><span :title="repo">{{ repo.split('/').pop() }}</span
          ><span class="count">{{ activeIssues.filter((i) => i.repo === repo).length }}</span>
        </button>
        <button
          v-for="project in boardProjects"
          :key="project.id"
          class="project-choice"
          :class="{ selected: selectedBoard?.id === project.id }"
          :title="`${project.owner} / ${project.name} (GitHub project)`"
          @click="boardParam = project.id"
        >
          <span aria-hidden="true">▦</span><span>{{ project.name }}</span
          ><span class="count">{{
            selectedBoard?.id === project.id
              ? boardIssues.filter((issue) => issue.state === 'open' && !isPr(issue)).length
              : ''
          }}</span>
        </button>
        <!-- Classic projects have no item API left; they still link out. -->
        <a
          v-for="project in classicProjects"
          :key="project.id"
          class="project-choice"
          :href="project.url"
          target="_blank"
          rel="noopener noreferrer"
          :title="`${project.owner} / ${project.name} (GitHub project)`"
        >
          <span aria-hidden="true">▦</span><span>{{ project.name }}</span
          ><span class="count">↗</span>
        </a>
        <div v-if="legacyRepos.length" class="workspace-empty" role="region" aria-label="Import">
          <p>
            This browser has {{ legacyRepos.length }} saved
            {{ legacyRepos.length === 1 ? 'repository' : 'repositories' }}. Import into this guild's
            shared list?
          </p>
          <button class="pixel-btn" @click="importLegacy">Import</button>
          <button class="pixel-btn" @click="legacyDismissed = true">Not now</button>
        </div>
        <div v-if="!repoNames.length" class="workspace-empty">
          <p>No repositories selected for browsing.</p>
          <button class="pixel-btn" @click="openSettings">+ Add repositories</button>
        </div>
      </div>
      <div class="project-art" aria-hidden="true"></div>
      <div class="project-connection" :class="{ connected: guildStore.isConnected }">
        ● {{ guildStore.isConnected ? 'Connected' : 'Disconnected' }}
      </div>
    </aside>

    <div class="project-main panel-bg" :class="{ 'show-detail': showDetail }">
      <header class="projects-heading">
        <h1>
          <span class="project-gem" aria-hidden="true">◆</span>
          {{ heading }}
        </h1>
        <a
          v-if="selectedBoard || selectedRepo"
          class="projects-heading-link"
          :href="selectedBoard ? selectedBoard.url : `https://github.com/${selectedRepo}`"
          target="_blank"
          rel="noopener noreferrer"
          >{{ selectedBoard ? 'Open in GitHub' : selectedRepo }} ↗</a
        >
      </header>
      <div class="project-kind-tabs" role="tablist" aria-label="Item type">
        <button
          v-for="option in kindOptions"
          :key="option.value"
          role="tab"
          class="project-kind-tab"
          :class="{ active: kind === option.value }"
          :aria-selected="kind === option.value"
          @click="kind = option.value"
        >
          {{ option.label }}
          <span class="project-kind-count">{{
            projectIssues.filter(
              (i) => isPr(i) === (option.value === 'pulls') && i.state === 'open',
            ).length
          }}</span>
        </button>
      </div>
      <div class="projects-toolbar">
        <div class="issue-status-filters" role="group" aria-label="State">
          <button
            v-for="option in stateOptions"
            :key="option.value"
            class="issue-filter"
            :class="{ selected: state === option.value }"
            :aria-pressed="state === option.value"
            @click="state = option.value"
          >
            <span class="issue-status-dot" :class="option.value" aria-hidden="true"></span
            >{{ option.label }}
            <span class="count">{{
              kindIssues.filter((i) => option.value === 'all' || i.state === option.value).length
            }}</span>
          </button>
        </div>
        <input
          v-model="search"
          type="search"
          :placeholder="kind === 'pulls' ? 'Search pull requests…' : 'Search issues…'"
          :aria-label="kind === 'pulls' ? 'Search pull requests' : 'Search issues'"
        />
        <select v-model="sort" aria-label="Sort">
          <option value="newest">Newest first</option>
          <option value="oldest">Oldest first</option>
        </select>
        <button
          class="pixel-btn"
          :disabled="listLoading || !ghStore.isConfigured"
          :aria-label="selectedBoard ? 'Sync' : 'Refresh'"
          @click="refresh"
        >
          {{ listLoading ? '…' : '↻' }}
        </button>
      </div>
      <div v-if="!ghStore.isConfigured" class="workspace-empty">
        Connect GitHub in your preferences to browse project issues.
      </div>
      <div v-else-if="listError" class="workspace-empty" role="alert">
        {{ listError }}
        <button class="pixel-btn" :disabled="listLoading" @click="refresh">Retry</button>
      </div>
      <div class="project-split">
        <div class="project-table-pane">
          <div class="project-table-scroll">
            <ul class="issue-list">
              <li
                v-for="row in pageRows"
                :key="row.issue.id"
                class="issue-row"
                :class="{ selected: selectedIssue?.id === row.issue.id, child: row.depth > 0 }"
                :style="{ '--depth': row.depth }"
              >
                <span v-if="row.depth" class="issue-tree" aria-hidden="true">└</span>
                <button
                  class="issue-select"
                  :aria-pressed="selectedIssue?.id === row.issue.id"
                  @click="selectIssue(row.issue.id)"
                >
                  <span class="issue-line">
                    <span class="issue-num">{{ row.issue.number }}</span>
                    <span class="issue-title" :class="{ closed: row.issue.state !== 'open' }">{{
                      row.issue.title
                    }}</span>
                  </span>
                  <span class="issue-meta">
                    <span class="project-state" :class="itemState(row.issue)">{{
                      itemState(row.issue)
                    }}</span>
                    <span v-if="statusName(row.issue)" class="board-status">{{
                      statusName(row.issue)
                    }}</span>
                    <span v-if="row.issue.sub_issues_summary?.total" class="sub-progress">
                      <span class="sub-bar" aria-hidden="true"
                        ><span
                          :style="{ width: `${row.issue.sub_issues_summary.percent_completed}%` }"
                        ></span></span
                      >{{ row.issue.sub_issues_summary.completed }}/{{
                        row.issue.sub_issues_summary.total
                      }}
                      sub-issues
                    </span>
                    <span v-if="!selectedRepo || selectedBoard" class="issue-repo"
                      >◆ {{ row.issue.repo.split('/').pop() }}</span
                    >
                    <span class="issue-created">{{ formatAge(row.issue.created_at) }}</span>
                  </span>
                </button>
              </li>
            </ul>
            <p v-if="!filteredIssues.length" class="workspace-empty">
              {{
                listLoading
                  ? 'Loading…'
                  : kind === 'pulls'
                    ? 'No matching pull requests.'
                    : 'No matching issues.'
              }}
            </p>
          </div>
          <footer class="project-pagination">
            <span
              >{{
                filteredIssues.length
                  ? `${(page - 1) * PAGE_SIZE + 1}–${Math.min(page * PAGE_SIZE, filteredIssues.length)} of ${filteredIssues.length}`
                  : '0'
              }}
              {{ kind === 'pulls' ? 'pull requests' : 'issues' }}</span
            >
            <button
              class="pixel-btn"
              :disabled="page === 1"
              aria-label="Previous page"
              @click="page--"
            >
              ‹
            </button>
            <span>{{ page }} / {{ pageCount }}</span>
            <button
              class="pixel-btn"
              :disabled="page >= pageCount"
              aria-label="Next page"
              @click="page++"
            >
              ›
            </button>
          </footer>
        </div>
        <div class="project-detail">
          <button class="project-back pixel-btn" @click="showDetail = false">
            ← {{ kind === 'pulls' ? 'Pull requests' : 'Issues' }}
          </button>
          <IssueViewer
            v-if="selectedIssue"
            :key="selectedIssue.id"
            :owner="selectedIssue.repo.split('/')[0]!"
            :repo="selectedIssue.repo.split('/')[1]!"
            :issue-number="selectedIssue.number"
          />
          <div v-else class="workspace-empty">
            Select an item to see its details, comments, and linked tasks.
          </div>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { readLegacyRepos, useGitHubStore } from '../stores/github'
import { useGuildStore } from '../stores/guild'
import { useUiStore } from '../stores/ui'
import { formatAge } from '../utils/format'
import IssueViewer from './IssueViewer.vue'
import { useQueryRef } from '../composables/useQueryRef'
import type { GitHubIssue } from '../types'

const ghStore = useGitHubStore()
const guildStore = useGuildStore()
const projectSearch = ref('')
const search = ref('')
// URL value of ?project=: '' (absent) = default, 'all' = All projects, else owner/repo.
const projectParam = ref('')
const selectedId = ref<number | null>(null)
const showDetail = ref(false)
const state = ref('open')
const sort = ref('newest')
const page = ref(1)
const PAGE_SIZE = 20
const kind = ref('issues')
const kindOptions = [
  { value: 'issues', label: 'Issues' },
  { value: 'pulls', label: 'Pull requests' },
]
useQueryRef(
  'kind',
  kind,
  'issues',
  kindOptions.map((o) => o.value),
)
const isPr = (issue: GitHubIssue) => !!issue.pull_request
function itemState(issue: GitHubIssue) {
  if (issue.pull_request?.merged_at) return 'merged'
  if (issue.draft && issue.state === 'open') return 'draft'
  return issue.state
}
// Projects v2 "status" arrives as {id, name, category}; the board cache sends a name.
function statusName(issue: GitHubIssue) {
  return typeof issue.status === 'string' ? issue.status : (issue.status?.name ?? '')
}
const stateOptions = [
  { value: 'all', label: 'All' },
  { value: 'open', label: 'Open' },
  { value: 'closed', label: 'Closed' },
]
useQueryRef('project', projectParam, '')
// ?board=<Projects v2 id> shows that linked board's cached issues instead of a repo.
const boardParam = ref('')
useQueryRef('board', boardParam, '')
const boardProjects = computed(() => ghStore.linkedProjects.filter((p) => p.kind === 'v2'))
const classicProjects = computed(() => ghStore.linkedProjects.filter((p) => p.kind !== 'v2'))
const selectedBoard = computed(() => boardProjects.value.find((p) => p.id === boardParam.value))
const boardIssues = ref<GitHubIssue[]>([])
const boardLoading = ref(false)
const boardError = ref('')
const listLoading = computed(() => (selectedBoard.value ? boardLoading.value : ghStore.loading))
const listError = computed(() => (selectedBoard.value ? boardError.value : ghStore.error))
let boardGeneration = 0
async function loadBoard(sync = false) {
  const board = selectedBoard.value
  const generation = ++boardGeneration
  boardError.value = ''
  if (!board) {
    boardIssues.value = []
    boardLoading.value = false
    return
  }
  boardLoading.value = true
  try {
    const issues = await ghStore.fetchProjectIssues(board.id, sync)
    if (generation === boardGeneration) boardIssues.value = issues
  } catch (e: unknown) {
    if (generation === boardGeneration)
      boardError.value = e instanceof Error ? e.message : String(e)
  } finally {
    if (generation === boardGeneration) boardLoading.value = false
  }
}
watch(
  () => selectedBoard.value?.id,
  () => loadBoard(),
  { immediate: true },
)
useQueryRef(
  'state',
  state,
  'open',
  stateOptions.map((o) => o.value),
)
const primaryRepo = computed(() => guildStore.currentGuild?.primary_repo ?? '')
// The primary repo is a default, never an override: URL param > guild primary repo.
// '' means All projects.
const selectedRepo = computed({
  get: () => (projectParam.value === 'all' ? '' : projectParam.value || primaryRepo.value),
  set: (repo: string) => {
    projectParam.value = repo === primaryRepo.value ? '' : repo || 'all'
  },
})
const repoNames = computed(() =>
  primaryRepo.value && !ghStore.selectedRepos.includes(primaryRepo.value)
    ? [primaryRepo.value, ...ghStore.selectedRepos]
    : ghStore.selectedRepos,
)
const filteredRepos = computed(() =>
  repoNames.value.filter((repo) => repo.toLowerCase().includes(projectSearch.value.toLowerCase())),
)
const scopedIssues = computed(() =>
  ghStore.issues.filter((issue) => repoNames.value.includes(issue.repo)),
)
const activeIssues = computed(() =>
  scopedIssues.value.filter((issue) => issue.state === 'open' && !isPr(issue)),
)
const projectIssues = computed(() =>
  selectedBoard.value
    ? boardIssues.value
    : scopedIssues.value.filter(
        (issue) => !selectedRepo.value || issue.repo === selectedRepo.value,
      ),
)
function selectRepo(repo: string) {
  boardParam.value = ''
  selectedRepo.value = repo
}
const heading = computed(() =>
  selectedBoard.value
    ? selectedBoard.value.name
    : selectedRepo.value
      ? selectedRepo.value.split('/').pop()!
      : 'All projects',
)
const kindIssues = computed(() =>
  projectIssues.value.filter((issue) => isPr(issue) === (kind.value === 'pulls')),
)
const filteredIssues = computed(() =>
  kindIssues.value
    .filter(
      (issue) =>
        (state.value === 'all' || issue.state === state.value) &&
        `${issue.number} ${issue.title}`.toLowerCase().includes(search.value.toLowerCase()),
    )
    .sort(
      (a, b) =>
        (sort.value === 'newest' ? -1 : 1) * (Date.parse(a.created_at) - Date.parse(b.created_at)),
    ),
)
const pageCount = computed(() => Math.max(1, Math.ceil(filteredIssues.value.length / PAGE_SIZE)))
// Sub-issues sit right under their parent when both are in the list.
// ponytail: children can land on the next page; page by top-level rows if that bothers anyone.
const rows = computed(() => {
  const listed = new Set(filteredIssues.value.map((i) => i.url))
  const children = new Map<string, GitHubIssue[]>()
  const roots: GitHubIssue[] = []
  for (const issue of filteredIssues.value) {
    const parent = issue.parent_issue_url
    if (parent && listed.has(parent)) children.set(parent, [...(children.get(parent) ?? []), issue])
    else roots.push(issue)
  }
  const out: Array<{ issue: GitHubIssue; depth: number }> = []
  const walk = (issue: GitHubIssue, depth: number) => {
    out.push({ issue, depth })
    for (const child of children.get(issue.url ?? '') ?? []) walk(child, depth + 1)
  }
  roots.forEach((issue) => walk(issue, 0))
  return out
})
const pageRows = computed(() =>
  rows.value.slice((page.value - 1) * PAGE_SIZE, page.value * PAGE_SIZE),
)
const pageIssues = computed(() => pageRows.value.map((row) => row.issue))
const selectedIssue = computed(
  () => filteredIssues.value.find((issue) => issue.id === selectedId.value) ?? pageIssues.value[0],
)

const scopeKey = computed(() => repoNames.value.join('\n'))
watch([scopeKey, selectedRepo, selectedBoard, kind, state, search, sort], () => {
  page.value = 1
  showDetail.value = false
  selectedId.value = null
})
watch(pageCount, (count) => {
  page.value = Math.min(page.value, count)
})
watch(page, () => {
  selectedId.value = null
})

function selectIssue(id: number) {
  selectedId.value = id
  showDetail.value = true
}

function refresh() {
  return selectedBoard.value ? loadBoard(true) : ghStore.fetchIssues(repoNames.value)
}
function openSettings() {
  useUiStore().settingsRequest = 'projects'
}

// Any scope change (edit, guild primary repo loading) refetches immediately.
watch(scopeKey, () => ghStore.fetchIssues(repoNames.value), { immediate: true })

// One-time, confirmed import of the pre-#1453 browser-global list into a guild
// that has never saved a shared list (browse_repos null).
const legacyDismissed = ref(false)
const legacyRepos = computed(() =>
  !legacyDismissed.value && guildStore.currentGuild?.browse_repos == null ? readLegacyRepos() : [],
)
async function importLegacy() {
  try {
    await ghStore.setSelectedRepos(legacyRepos.value)
    localStorage.removeItem('gh_repos')
    projectParam.value = ''
  } catch {
    openSettings()
  }
}

let refreshTimer: ReturnType<typeof setInterval> | undefined
onMounted(() => {
  refreshTimer = setInterval(() => ghStore.fetchIssues(repoNames.value, true), 180_000)
})
onUnmounted(() => clearInterval(refreshTimer))
</script>
