<template>
  <div class="spawn-form">
    <div v-if="launched" class="spawn-success">
      <span class="spawn-success-label">Launched</span>
      <span class="spawn-success-id">{{ launchedWorkerId }}</span>
    </div>
    <template v-else>
      <label class="spawn-label">Repos</label>
      <div v-if="loadingRepos" class="spawn-hint-text">Loading repos…</div>
      <div v-else-if="ghStore.repos.length === 0" class="spawn-hint-text">
        No repos found — configure GitHub first.
      </div>
      <div v-else class="spawn-repo-list">
        <template v-for="group in groupedRepos" :key="group.owner">
          <label
            class="spawn-repo-row spawn-org-row"
            :class="{ selected: orgAllSelected(group.owner) }"
          >
            <input
              type="checkbox"
              :ref="(el) => setOrgCheckboxRef(el as HTMLInputElement | null, group.owner)"
              :checked="orgAllSelected(group.owner)"
              @change="toggleOrg(group.owner)"
              class="spawn-repo-check"
            />
            <span class="spawn-repo-name spawn-org-name">{{ group.owner }}</span>
          </label>
          <label
            v-for="repo in group.repos"
            :key="repo.full_name"
            class="spawn-repo-row spawn-repo-indent"
            :class="{ selected: selectedRepos.includes(repo.full_name) }"
          >
            <input
              type="checkbox"
              :checked="selectedRepos.includes(repo.full_name)"
              @change="toggleRepo(repo.full_name)"
              class="spawn-repo-check"
            />
            <span class="spawn-repo-name">{{ repo.full_name.split('/')[1] }}</span>
          </label>
        </template>
      </div>
      <label class="spawn-label">Name <span class="spawn-hint">(optional)</span></label>
      <input v-model="name" class="spawn-input" type="text" placeholder="auto-generated" />
      <label class="spawn-label">Tools <span class="spawn-hint">(optional)</span></label>
      <div class="spawn-tool-list">
        <label
          v-for="tool in AVAILABLE_TOOLS"
          :key="tool"
          class="spawn-repo-row"
          :class="{ selected: selectedTools.includes(tool) }"
        >
          <input
            type="checkbox"
            :checked="selectedTools.includes(tool)"
            @change="toggleTool(tool)"
            class="spawn-repo-check"
          />
          <span class="spawn-repo-name">{{ tool }}</span>
        </label>
      </div>
      <label class="spawn-label">Agents <span class="spawn-hint">(optional)</span></label>
      <input
        v-model.number="agentCount"
        class="spawn-input spawn-input--narrow"
        type="number"
        min="1"
        max="16"
        placeholder="4"
      />
      <div class="spawn-actions">
        <button
          class="pixel-btn spawn-launch-btn"
          :disabled="spawning || selectedRepos.length === 0"
          @click="launch"
        >
          {{ spawning ? 'Launching…' : 'Launch' }}
        </button>
      </div>
      <div v-if="error" class="spawn-error">{{ error }}</div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useGuildStore } from '../../stores/guild'
import { useGitHubStore } from '../../stores/github'
import { api } from '../../utils/api'
import { groupAndSortRepos } from '../../utils/repoGroups'

const emit = defineEmits<{ (e: 'launched'): void }>()

const guildStore = useGuildStore()
const ghStore = useGitHubStore()

const AVAILABLE_TOOLS = ['claude', 'codex', 'pi'] as const

const selectedRepos = ref<string[]>([])
const name = ref('')
const selectedTools = ref<string[]>([])
const agentCount = ref<number | null>(null)
const spawning = ref(false)
const error = ref('')
const loadingRepos = ref(false)
const launched = ref(false)
const launchedWorkerId = ref('')

const groupedRepos = computed(() => groupAndSortRepos(ghStore.repos))

onMounted(async () => {
  selectedRepos.value = [...ghStore.selectedRepos]
  if (ghStore.repos.length === 0 && ghStore.token) {
    loadingRepos.value = true
    await ghStore.fetchRepos()
    loadingRepos.value = false
  }
})

function toggleRepo(fullName: string) {
  const idx = selectedRepos.value.indexOf(fullName)
  if (idx >= 0) {
    selectedRepos.value.splice(idx, 1)
  } else {
    selectedRepos.value.push(fullName)
  }
}

function orgAllSelected(owner: string): boolean {
  const repos = groupedRepos.value.find(g => g.owner === owner)?.repos ?? []
  return repos.length > 0 && repos.every(r => selectedRepos.value.includes(r.full_name))
}

function orgSomeSelected(owner: string): boolean {
  const repos = groupedRepos.value.find(g => g.owner === owner)?.repos ?? []
  return repos.some(r => selectedRepos.value.includes(r.full_name))
}

function toggleOrg(owner: string) {
  const repos = groupedRepos.value.find(g => g.owner === owner)?.repos ?? []
  if (orgAllSelected(owner)) {
    const names = new Set(repos.map(r => r.full_name))
    selectedRepos.value = selectedRepos.value.filter(n => !names.has(n))
  } else {
    for (const repo of repos) {
      if (!selectedRepos.value.includes(repo.full_name)) {
        selectedRepos.value.push(repo.full_name)
      }
    }
  }
}

function setOrgCheckboxRef(el: HTMLInputElement | null, owner: string) {
  if (el) {
    el.indeterminate = orgSomeSelected(owner) && !orgAllSelected(owner)
  }
}

function toggleTool(tool: string) {
  const idx = selectedTools.value.indexOf(tool)
  if (idx >= 0) {
    selectedTools.value.splice(idx, 1)
  } else {
    selectedTools.value.push(tool)
  }
}

async function launch() {
  const guild = guildStore.currentGuild
  if (!guild || selectedRepos.value.length === 0) return
  spawning.value = true
  error.value = ''
  try {
    const result = await api<{ worker_id?: string }>(`/guilds/${guild.id}/spawn-worker`, {
      method: 'POST',
      json: {
        repos: selectedRepos.value,
        name: name.value.trim() || undefined,
        tools: selectedTools.value.length ? selectedTools.value : undefined,
        agent_count: agentCount.value ?? undefined,
      },
    })
    launchedWorkerId.value = result?.worker_id ?? ''
    launched.value = true
    setTimeout(() => emit('launched'), 2500)
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    spawning.value = false
  }
}
</script>

<style scoped>
.spawn-form {
  padding: 8px 10px;
  border-bottom: 1px solid var(--color-brass-dark);
  background: var(--color-bg-secondary);
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.spawn-success {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 12px 4px;
}

.spawn-success-label {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-green, #4caf50);
  letter-spacing: 1.5px;
  text-transform: uppercase;
}

.spawn-success-id {
  font-family: var(--font-mono, monospace);
  font-size: 11px;
  color: var(--color-teal);
  letter-spacing: 0.5px;
}

.spawn-label {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-teal);
  letter-spacing: 1px;
  text-transform: uppercase;
}

.spawn-hint {
  color: var(--color-text-dim);
  font-family: var(--font-mono, monospace);
  text-transform: none;
  letter-spacing: 0;
  font-size: 8px;
}

.spawn-input {
  background: var(--color-bg);
  border: 1px solid var(--color-brass-dark);
  color: var(--color-text);
  font-family: var(--font-mono, monospace);
  font-size: 10px;
  padding: 4px 6px;
  outline: none;
  border-radius: 2px;
  width: 100%;
  box-sizing: border-box;
}

.spawn-input:focus {
  border-color: var(--color-teal);
}

.spawn-input--narrow {
  width: 80px;
}

.spawn-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 2px;
}

.spawn-launch-btn {
  font-size: 7px;
  padding: 4px 10px;
}

.spawn-launch-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.spawn-error {
  font-size: 10px;
  color: var(--color-red);
  word-break: break-word;
}

.spawn-hint-text {
  font-size: 10px;
  color: var(--color-text-dim);
  padding: 4px 0;
  font-style: italic;
}

.spawn-repo-list {
  display: flex;
  flex-direction: column;
  gap: 1px;
  max-height: 160px;
  overflow-y: auto;
  border: 1px solid var(--color-brass-dark);
  border-radius: 2px;
  background: var(--color-bg);
}

.spawn-tool-list {
  display: flex;
  flex-direction: row;
  gap: 2px;
  border: 1px solid var(--color-brass-dark);
  border-radius: 2px;
  background: var(--color-bg);
}

.spawn-repo-row {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 5px 7px;
  cursor: pointer;
  border: 1px solid transparent;
  transition: background 0.1s;
}

.spawn-repo-row:hover {
  background: var(--color-bg-tertiary);
}

.spawn-repo-row.selected {
  background: rgba(232, 170, 0, 0.08);
  border-color: var(--color-brass-dark);
}

.spawn-org-row {
  background: var(--color-bg-secondary);
  border-bottom: 1px solid var(--color-brass-dark);
}

.spawn-org-row.selected {
  background: rgba(232, 170, 0, 0.12);
}

.spawn-org-name {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-teal);
  letter-spacing: 0.5px;
}

.spawn-repo-indent {
  padding-left: 22px;
}

.spawn-repo-check {
  accent-color: var(--color-brass);
  width: 12px;
  height: 12px;
  cursor: pointer;
  flex-shrink: 0;
}

.spawn-repo-name {
  font-size: 10px;
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
