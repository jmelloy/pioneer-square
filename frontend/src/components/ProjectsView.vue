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
        <p v-if="!repoNames.length" class="workspace-empty">
          Choose repositories in guild settings to see their issues.
        </p>
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
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useGitHubStore } from '../stores/github'
import { useGuildStore } from '../stores/guild'
import { formatAge } from '../utils/format'
import IssueViewer from './IssueViewer.vue'

const ghStore = useGitHubStore()
const guildStore = useGuildStore()
const projectSearch = ref('')
const search = ref('')
const selectedRepo = ref('')
const selectedId = ref<number | null>(null)
const showDetail = ref(false)
const state = ref('all')
const sort = ref('newest')
const page = ref(1)
const PAGE_SIZE = 20
const stateOptions = [
  { value: 'all', label: 'All' },
  { value: 'open', label: 'Open' },
  { value: 'closed', label: 'Closed' },
]
const repoNames = computed(() =>
  guildStore.currentGuild?.primary_repo
    ? [guildStore.currentGuild.primary_repo]
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
let refreshTimer: ReturnType<typeof setInterval> | undefined
onMounted(() => {
  refresh()
  refreshTimer = setInterval(() => ghStore.fetchIssues(repoNames.value, true), 180_000)
})
onUnmounted(() => clearInterval(refreshTimer))
</script>
