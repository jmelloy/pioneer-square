<template>
  <div class="projects-settings">
    <h3 class="ps-heading">Repositories</h3>
    <p class="repo-editor-hint">
      Changes which repositories the Projects screen lists for everyone in this guild. The Foreman's
      primary repo and worker repository access are configured separately.
    </p>
    <p v-if="!ghStore.isConfigured" class="repo-editor-hint">
      Connect GitHub in your preferences to edit repositories and projects.
    </p>
    <template v-else>
      <p v-if="primaryRepo" class="primary-override">
        The guild's primary repo <strong>{{ primaryRepo }}</strong> is always listed and is the
        default project.
      </p>
      <h4 class="ps-sub">Selected ({{ draft.length }})</h4>
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
        v-model="repoQuery"
        class="settings-input"
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
      <p v-if="repoSaveError" class="workspace-empty" role="alert">{{ repoSaveError }}</p>
      <div class="ps-footer">
        <button class="pixel-btn" :disabled="!repoDirty" @click="saveRepos">
          Save repositories
        </button>
      </div>

      <h3 class="ps-heading">GitHub Projects</h3>
      <p class="repo-editor-hint">
        Link GitHub Projects (user, organization, and repository projects, including Projects v2) to
        show them in the Projects screen alongside repositories.
      </p>
      <h4 class="ps-sub">Linked ({{ projectDraft.length }})</h4>
      <ul class="repo-editor-list" aria-label="Linked projects">
        <li v-for="project in projectDraft" :key="project.id">
          <span :title="project.url">{{ project.owner }} / {{ project.name }}</span>
          <button
            class="pixel-btn"
            :aria-label="`Unlink ${project.name}`"
            @click="unlinkProject(project.id)"
          >
            ✕
          </button>
        </li>
        <li v-if="!projectDraft.length" class="workspace-empty">No projects linked.</li>
      </ul>
      <p v-if="projectsLoading" class="workspace-empty" role="status">Loading projects…</p>
      <div v-else-if="!projectsLoaded" class="ps-footer">
        <button class="pixel-btn" @click="loadProjects">Find GitHub projects</button>
      </div>
      <ul v-else class="repo-editor-list" aria-label="Available projects">
        <li v-for="project in projectMatches" :key="project.id">
          <span :title="project.url"
            >{{ project.owner }} / {{ project.name }} ({{ project.kind }})</span
          >
          <button
            class="pixel-btn"
            :aria-label="`Link ${project.name}`"
            @click="projectDraft.push(project)"
          >
            +
          </button>
        </li>
        <li v-if="!projectMatches.length" class="workspace-empty">No more projects found.</li>
      </ul>
      <p v-if="projectSaveError" class="workspace-empty" role="alert">{{ projectSaveError }}</p>
      <div class="ps-footer">
        <button class="pixel-btn" :disabled="!projectsDirty" @click="saveProjects">
          Save projects
        </button>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useGitHubStore } from '../stores/github'
import { useGuildStore } from '../stores/guild'
import type { GitHubProject } from '../types'

const ghStore = useGitHubStore()
const guildStore = useGuildStore()
const primaryRepo = computed(() => guildStore.currentGuild?.primary_repo ?? '')

const draft = ref<string[]>([...ghStore.selectedRepos])
const repoQuery = ref('')
const reposLoading = ref(false)
const repoError = ref('')
const repoSaveError = ref('')
const repoDirty = computed(
  () => JSON.stringify(draft.value) !== JSON.stringify(ghStore.selectedRepos),
)
const repoMatches = computed(() =>
  ghStore.repos
    .map((repo) => repo.full_name)
    .filter(
      (name) =>
        !draft.value.includes(name) && name.toLowerCase().includes(repoQuery.value.toLowerCase()),
    )
    // capped render; search narrows the full paginated list
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

function addRepo(repo: string) {
  draft.value.push(repo)
  repoQuery.value = ''
}

function removeRepo(repo: string) {
  draft.value = draft.value.filter((name) => name !== repo)
}

async function saveRepos() {
  repoSaveError.value = ''
  try {
    await ghStore.setSelectedRepos(draft.value)
  } catch (e: unknown) {
    repoSaveError.value = e instanceof Error ? e.message : String(e)
  }
}

const projectDraft = ref<GitHubProject[]>([...ghStore.linkedProjects])
const available = ref<GitHubProject[]>([])
const projectsLoading = ref(false)
const projectsLoaded = ref(false)
const projectSaveError = ref('')
const projectsDirty = computed(
  () =>
    JSON.stringify(projectDraft.value.map((p) => p.id)) !==
    JSON.stringify(ghStore.linkedProjects.map((p) => p.id)),
)
const projectMatches = computed(() =>
  available.value.filter((p) => !projectDraft.value.some((d) => d.id === p.id)),
)

async function loadProjects() {
  projectsLoading.value = true
  const names = [...new Set([primaryRepo.value, ...draft.value].filter(Boolean))]
  available.value = await ghStore.fetchAvailableProjects(names)
  projectsLoaded.value = true
  projectsLoading.value = false
}

function unlinkProject(id: string) {
  projectDraft.value = projectDraft.value.filter((p) => p.id !== id)
}

async function saveProjects() {
  projectSaveError.value = ''
  try {
    await ghStore.setLinkedProjects(projectDraft.value)
  } catch (e: unknown) {
    projectSaveError.value = e instanceof Error ? e.message : String(e)
  }
}

// Keep drafts in sync if the guild changes underneath the panel.
watch(
  () => guildStore.currentGuild?.id,
  () => {
    draft.value = [...ghStore.selectedRepos]
    projectDraft.value = [...ghStore.linkedProjects]
  },
)

onMounted(() => {
  if (ghStore.isConfigured && !ghStore.repos.length) loadRepos()
})
</script>

<style scoped>
.projects-settings {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.ps-heading,
.ps-sub {
  margin: 0;
  color: var(--color-brass-light);
}
.ps-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
