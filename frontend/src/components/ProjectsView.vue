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
          :class="{ selected: !selectedRepo }"
          @click="selectedRepo = ''"
        >
          <span aria-hidden="true">◈</span><span>All projects</span
          ><span class="count">{{ scopedIssues.length }}</span>
        </button>
        <button
          v-for="repo in filteredRepos"
          :key="repo"
          class="project-choice"
          :class="{ selected: selectedRepo === repo }"
          @click="selectedRepo = repo"
        >
          <span class="project-gem" aria-hidden="true">◆</span
          ><span :title="repo">{{ repo.split('/').pop() }}</span
          ><span class="count">{{ scopedIssues.filter((i) => i.repo === repo).length }}</span>
        </button>
        <div v-if="!repoNames.length" class="workspace-empty">
          <p>No repositories selected for browsing.</p>
          <button class="pixel-btn" @click="openEditor">+ Add repositories</button>
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
        <h1><span class="project-gem" aria-hidden="true">◆</span> Projects / Issues</h1>
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
        <button
          class="pixel-btn"
          :disabled="ghStore.loading || !ghStore.isConfigured"
          @click="refresh"
        >
          {{ ghStore.loading ? 'Loading…' : '↻ Refresh' }}
        </button>
        <button class="pixel-btn" :disabled="!ghStore.isConfigured" @click="openEditor">
          ✎ Repositories
        </button>
      </header>
      <div v-if="!ghStore.isConfigured" class="workspace-empty">
        Connect GitHub in your preferences to browse project issues.
      </div>
      <div v-else-if="ghStore.error" class="workspace-empty" role="alert">{{ ghStore.error }}</div>
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
                  </td>
                  <td class="issue-created">{{ formatAge(issue.created_at) }}</td>
                </tr>
              </tbody>
            </table>
            <p v-if="!filteredIssues.length" class="workspace-empty">
              {{ ghStore.loading ? 'Loading issues…' : 'No matching issues.' }}
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
    <div v-if="editing" class="repo-editor-backdrop" @click.self="cancelEdit">
      <div
        class="repo-editor panel-bg"
        role="dialog"
        aria-modal="true"
        aria-labelledby="repo-editor-title"
        @keydown.esc="cancelEdit"
      >
        <h2 id="repo-editor-title">Browse repositories</h2>
        <p class="repo-editor-hint">
          Only changes which repositories this screen lists. The Foreman's primary repo and worker
          repository access are configured separately.
        </p>
        <p v-if="primaryRepo" class="primary-override">
          The guild's primary repo <strong>{{ primaryRepo }}</strong> is always listed and is the
          default project.
        </p>
        <h3>Selected ({{ draft.length }})</h3>
        <ul class="repo-editor-list" aria-label="Selected repositories">
          <li v-for="repo in draft" :key="repo">
            <span :title="repo">{{ repo }}</span>
            <button class="pixel-btn" :aria-label="`Remove ${repo}`" @click="removeRepo(repo)">
              ✕
            </button>
          </li>
          <li v-if="!draft.length" class="workspace-empty">
            Nothing selected yet — search below to add repositories.
          </li>
        </ul>
        <input
          ref="repoQueryInput"
          v-model="repoQuery"
          type="search"
          placeholder="Search your GitHub repositories…"
          aria-label="Search GitHub repositories"
          @keydown.enter.prevent="repoMatches[0] && addRepo(repoMatches[0])"
        />
        <p v-if="reposLoading" class="workspace-empty" role="status">Loading repositories…</p>
        <div v-else-if="repoError" class="workspace-empty" role="alert">
          {{ repoError }}
          <button class="pixel-btn" @click="loadRepos">Retry</button>
        </div>
        <ul v-else class="repo-editor-list" aria-label="Available repositories">
          <li v-for="repo in repoMatches" :key="repo">
            <span :title="repo">{{ repo }}</span>
            <button class="pixel-btn" :aria-label="`Add ${repo}`" @click="addRepo(repo)">+</button>
          </li>
          <li v-if="!repoMatches.length" class="workspace-empty">No matching repositories.</li>
        </ul>
        <footer>
          <button class="pixel-btn" @click="cancelEdit">Cancel</button>
          <button class="pixel-btn" @click="saveRepos">Save</button>
        </footer>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useGitHubStore } from '../stores/github'
import { useGuildStore } from '../stores/guild'
import { formatAge } from '../utils/format'
import IssueViewer from './IssueViewer.vue'
import { useQueryRef } from '../composables/useQueryRef'

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
  scopedIssues.value.filter((issue) => !selectedRepo.value || issue.repo === selectedRepo.value),
)
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

watch([selectedRepo, state, search, sort], () => {
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
  return ghStore.fetchIssues(repoNames.value)
}
const editing = ref(false)
const draft = ref<string[]>([])
const repoQuery = ref('')
const repoQueryInput = ref<HTMLInputElement>()
const reposLoading = ref(false)
const repoError = ref('')
const repoMatches = computed(() =>
  ghStore.repos
    .map((repo) => repo.full_name)
    .filter(
      (name) =>
        !draft.value.includes(name) && name.toLowerCase().includes(repoQuery.value.toLowerCase()),
    )
    // ponytail: capped render; fetchRepos only returns the first 100 anyway
    .slice(0, 50),
)

async function loadRepos() {
  // fetchRepos reports failures via the shared store error; read it, then restore the issues error.
  const previousError = ghStore.error
  ghStore.error = ''
  reposLoading.value = true
  repoError.value = ''
  await ghStore.fetchRepos()
  repoError.value = ghStore.error
  ghStore.error = previousError
  reposLoading.value = false
}

function openEditor() {
  draft.value = [...ghStore.selectedRepos]
  repoQuery.value = ''
  editing.value = true
  nextTick(() => repoQueryInput.value?.focus())
  if (!ghStore.repos.length) loadRepos()
}

function addRepo(repo: string) {
  draft.value.push(repo)
  repoQuery.value = ''
}

function removeRepo(repo: string) {
  draft.value = draft.value.filter((name) => name !== repo)
}

function cancelEdit() {
  editing.value = false
}

function saveRepos() {
  ghStore.setSelectedRepos(draft.value)
  editing.value = false
  projectParam.value = ''
  refresh()
}

let refreshTimer: ReturnType<typeof setInterval> | undefined
onMounted(() => {
  refresh()
  refreshTimer = setInterval(() => ghStore.fetchIssues(repoNames.value, true), 180_000)
})
onUnmounted(() => clearInterval(refreshTimer))
</script>
