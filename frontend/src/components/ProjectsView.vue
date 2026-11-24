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
          ><span class="count">{{ scopedIssues.length }}</span>
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
          ><span class="count">{{ scopedIssues.filter((i) => i.repo === repo).length }}</span>
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
            selectedBoard?.id === project.id ? boardIssues.length : ''
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
      <div class="issue-status-filters">
        <h3>☷ All issues</h3>
        <button
          v-for="option in stateOptions"
          :key="option.value"
          class="project-choice"
          :class="{ selected: state === option.value }"
          @click="state = option.value"
        >
          <span class="issue-status-dot" :class="option.value" aria-hidden="true"></span
          ><span>{{ option.label }}</span
          ><span class="count">{{
            projectIssues.filter((i) => option.value === 'all' || i.state === option.value).length
          }}</span>
        </button>
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
          {{ selectedBoard ? selectedBoard.name : 'Projects / Issues' }}
        </h1>
        <input
          v-model="search"
          type="search"
          placeholder="Search issues…"
          aria-label="Search issues"
        />
        <select v-model="sort" aria-label="Sort issues">
          <option value="newest">Newest first</option>
          <option value="oldest">Oldest first</option>
        </select>
        <button class="pixel-btn" :disabled="listLoading || !ghStore.isConfigured" @click="refresh">
          {{ listLoading ? 'Loading…' : selectedBoard ? '↻ Sync' : '↻ Refresh' }}
        </button>
        <a
          v-if="selectedBoard"
          class="pixel-btn"
          :href="selectedBoard.url"
          target="_blank"
          rel="noopener noreferrer"
          >↗ Open in GitHub</a
        >
        <button class="pixel-btn" :disabled="!ghStore.isConfigured" @click="openSettings">
          ⚙ Manage in settings
        </button>
      </header>
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
            <table class="project-table">
              <thead>
                <tr>
                  <th scope="col">#</th>
                  <th scope="col">Title</th>
                  <th scope="col">Project</th>
                  <th scope="col">Status</th>
                  <th scope="col">Created</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="issue in pageIssues"
                  :key="issue.id"
                  :class="{ selected: selectedIssue?.id === issue.id }"
                >
                  <td>{{ issue.number }}</td>
                  <td>
                    <button
                      class="issue-select"
                      :aria-pressed="selectedIssue?.id === issue.id"
                      @click="selectIssue(issue.id)"
                    >
                      {{ issue.title }}
                    </button>
                  </td>
                  <td>
                    <span class="project-gem" aria-hidden="true">◆</span>
                    {{ issue.repo.split('/').pop() }}
                  </td>
                  <td>
                    <span class="project-state" :class="issue.state">{{ issue.state }}</span>
                    <span v-if="issue.status" class="board-status">{{ issue.status }}</span>
                  </td>
                  <td class="issue-created">{{ formatAge(issue.created_at) }}</td>
                </tr>
              </tbody>
            </table>
            <p v-if="!filteredIssues.length" class="workspace-empty">
              {{ listLoading ? 'Loading issues…' : 'No matching issues.' }}
            </p>
          </div>
          <footer class="project-pagination">
            <span
              >{{
                filteredIssues.length
                  ? `${(page - 1) * PAGE_SIZE + 1}–${Math.min(page * PAGE_SIZE, filteredIssues.length)} of ${filteredIssues.length}`
                  : '0'
              }}
              issues</span
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
          <button class="project-back pixel-btn" @click="showDetail = false">← Issues</button>
          <IssueViewer
            v-if="selectedIssue"
            :key="selectedIssue.id"
            :owner="selectedIssue.repo.split('/')[0]!"
            :repo="selectedIssue.repo.split('/')[1]!"
            :issue-number="selectedIssue.number"
          />
          <div v-else class="workspace-empty">
            Select an issue to see its details, comments, and linked tasks.
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
const filteredIssues = computed(() =>
  projectIssues.value
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
const pageIssues = computed(() =>
  filteredIssues.value.slice((page.value - 1) * PAGE_SIZE, page.value * PAGE_SIZE),
)
const selectedIssue = computed(
  () => filteredIssues.value.find((issue) => issue.id === selectedId.value) ?? pageIssues.value[0],
)

const scopeKey = computed(() => repoNames.value.join('\n'))
watch([scopeKey, selectedRepo, selectedBoard, state, search, sort], () => {
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
