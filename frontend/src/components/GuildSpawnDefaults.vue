<template>
  <div class="gsd">
    <div v-if="loading" class="gsd-hint">Loading…</div>
    <template v-else>
      <p class="gsd-intro">
        The guild baseline used when the foreman spawns a worker and pre-filled into each member's
        spawn form. Members can override it per launch.
      </p>

      <template v-if="canManage">
        <label class="gsd-sublabel">Repos</label>
        <div v-if="ghStore.repos.length === 0" class="gsd-hint">
          No repos loaded — configure GitHub first.
        </div>
        <div v-else class="gsd-repo-list">
          <template v-for="group in groupedRepos" :key="group.owner">
            <label
              class="gsd-repo-row gsd-org-row"
              :class="{ selected: orgAllSelected(group.owner) }"
            >
              <input
                type="checkbox"
                :ref="(el) => setOrgCheckboxRef(el as HTMLInputElement | null, group.owner)"
                :checked="orgAllSelected(group.owner)"
                @change="toggleOrg(group.owner)"
                class="gsd-check"
              />
              <span class="gsd-repo-name gsd-org-name">{{ group.owner }}</span>
            </label>
            <label
              v-for="repo in group.repos"
              :key="repo.full_name"
              class="gsd-repo-row gsd-repo-indent"
              :class="{ selected: repos.includes(repo.full_name) }"
            >
              <input
                type="checkbox"
                :checked="repos.includes(repo.full_name)"
                @change="toggleRepo(repo.full_name)"
                class="gsd-check"
              />
              <span class="gsd-repo-name">{{ repo.full_name.split('/')[1] }}</span>
            </label>
          </template>
        </div>

        <label class="gsd-sublabel">Tools</label>
        <div class="gsd-tool-list">
          <label
            v-for="tool in AVAILABLE_TOOLS"
            :key="tool"
            class="gsd-repo-row"
            :class="{ selected: tools.includes(tool) }"
          >
            <input
              type="checkbox"
              :checked="tools.includes(tool)"
              @change="toggleTool(tool)"
              class="gsd-check"
            />
            <span class="gsd-repo-name">{{ tool }}</span>
          </label>
        </div>

        <label class="gsd-sublabel">Agents</label>
        <input
          v-model.number="agentCount"
          class="gsd-input gsd-input--narrow"
          type="number"
          min="1"
          max="16"
          placeholder="worker default"
        />

        <div class="gsd-actions">
          <button class="pixel-btn gsd-save-btn" :disabled="saving" @click="save">
            {{ saving ? 'Saving…' : 'Save' }}
          </button>
          <span v-if="status" class="save-status" :class="'save-status-' + status">
            {{ status === 'saved' ? 'Saved' : 'Error' }}
          </span>
        </div>
        <div v-if="errorMsg" class="gsd-error">{{ errorMsg }}</div>
      </template>

      <div v-else class="gsd-readonly">
        <div class="gsd-ro-field">
          <span class="gsd-ro-label">Repos</span>
          <span class="gsd-ro-value">{{ repos.length ? repos.join(', ') : '—' }}</span>
        </div>
        <div class="gsd-ro-field">
          <span class="gsd-ro-label">Tools</span>
          <span class="gsd-ro-value">{{ tools.length ? tools.join(', ') : '—' }}</span>
        </div>
        <div class="gsd-ro-field">
          <span class="gsd-ro-label">Agents</span>
          <span class="gsd-ro-value">{{ agentCount ?? '—' }}</span>
        </div>
        <p class="gsd-hint">Only owners can change the guild spawn defaults.</p>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { api, ApiError } from '../utils/api'
import { useAuthStore } from '../stores/auth'
import { useGitHubStore } from '../stores/github'
import { useRepoSelection } from '../composables/useRepoSelection'

const AVAILABLE_TOOLS = ['claude', 'codex', 'pi'] as const

interface SpawnDefaults {
  repos: string[]
  tools: string[]
  agent_count: number | null
}

interface GuildMemberRow {
  user_id: string
  role: string
}

const props = defineProps<{ guildId: string }>()

const authStore = useAuthStore()
const ghStore = useGitHubStore()

const loading = ref(true)
const saving = ref(false)
const status = ref<'' | 'saved' | 'error'>('')
const errorMsg = ref('')

const repos = ref<string[]>([])
const tools = ref<string[]>([])
const agentCount = ref<number | null>(null)
const myRole = ref<string | null>(null)

const canManage = computed(() => myRole.value === 'owner')

// The repo/org/tool checkbox behaviour, shared with the Launch form and User
// Preferences instead of copy-pasted into each (#1240).
const { groupedRepos, toggleRepo, orgAllSelected, toggleOrg, setOrgCheckboxRef, toggleTool } =
  useRepoSelection(repos, tools)

let statusTimer: ReturnType<typeof setTimeout> | null = null

onMounted(async () => {
  if (ghStore.repos.length === 0 && ghStore.token) {
    try {
      await ghStore.fetchRepos()
    } catch {
      /* non-fatal: repo checkboxes just won't render */
    }
  }
  await Promise.all([loadRole(), loadDefaults()])
  loading.value = false
})

async function loadRole() {
  try {
    const members = await api<GuildMemberRow[]>(
      `/api/guilds/${encodeURIComponent(props.guildId)}/members`,
    )
    myRole.value = members.find((m) => m.user_id === authStore.user?.id)?.role ?? null
  } catch {
    myRole.value = null
  }
}

async function loadDefaults() {
  try {
    const d = await api<SpawnDefaults>(
      `/guilds/${encodeURIComponent(props.guildId)}/spawn-defaults`,
    )
    repos.value = d.repos ?? []
    tools.value = d.tools ?? []
    agentCount.value = d.agent_count ?? null
  } catch {
    repos.value = []
    tools.value = []
    agentCount.value = null
  }
}

async function save() {
  saving.value = true
  status.value = ''
  errorMsg.value = ''
  try {
    const saved = await api<SpawnDefaults>(
      `/guilds/${encodeURIComponent(props.guildId)}/spawn-defaults`,
      {
        method: 'PUT',
        json: {
          repos: repos.value,
          tools: tools.value,
          agent_count: agentCount.value ?? null,
        },
      },
    )
    repos.value = saved.repos ?? []
    tools.value = saved.tools ?? []
    agentCount.value = saved.agent_count ?? null
    status.value = 'saved'
  } catch (e) {
    status.value = 'error'
    errorMsg.value = e instanceof ApiError ? e.message : 'Failed to save'
  } finally {
    saving.value = false
    if (statusTimer) clearTimeout(statusTimer)
    statusTimer = setTimeout(() => {
      status.value = ''
    }, 2000)
  }
}
</script>

<style scoped>
.gsd {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 6px;
  padding: 10px;
  background: var(--color-bg);
  border: 1px solid var(--color-brass-dark);
  border-radius: 2px;
}

.gsd-intro {
  font-size: 9px;
  color: var(--color-text-dim);
  font-style: italic;
  margin: 0;
  line-height: 1.4;
}

.gsd-sublabel {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-teal);
  letter-spacing: 1px;
  text-transform: uppercase;
  margin-top: 2px;
}

.gsd-hint {
  font-size: 10px;
  color: var(--color-text-dim);
  font-style: italic;
}

.gsd-repo-list {
  display: flex;
  flex-direction: column;
  gap: 1px;
  max-height: 140px;
  overflow-y: auto;
  border: 1px solid var(--color-brass-dark);
  border-radius: 2px;
  background: var(--color-bg-secondary);
}

.gsd-tool-list {
  display: flex;
  flex-direction: row;
  gap: 2px;
  border: 1px solid var(--color-brass-dark);
  border-radius: 2px;
  background: var(--color-bg-secondary);
}

.gsd-repo-row {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 5px 7px;
  cursor: pointer;
  border: 1px solid transparent;
  transition: background 0.1s;
}

.gsd-repo-row:hover {
  background: var(--color-bg-tertiary);
}

.gsd-repo-row.selected {
  background: rgba(232, 170, 0, 0.08);
  border-color: var(--color-brass-dark);
}

.gsd-org-row {
  background: var(--color-bg-secondary);
  border-bottom: 1px solid var(--color-brass-dark);
}

.gsd-org-row.selected {
  background: rgba(232, 170, 0, 0.12);
}

.gsd-org-name {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-teal);
  letter-spacing: 0.5px;
}

.gsd-repo-indent {
  padding-left: 22px;
}

.gsd-check {
  accent-color: var(--color-brass);
  width: 12px;
  height: 12px;
  cursor: pointer;
  flex-shrink: 0;
}

.gsd-repo-name {
  font-size: 10px;
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.gsd-input {
  background: var(--color-bg-secondary);
  border: 1px solid var(--color-brass-dark);
  color: var(--color-text);
  font-family: var(--font-mono, monospace);
  font-size: 11px;
  padding: 5px 7px;
  outline: none;
  border-radius: 2px;
  box-sizing: border-box;
}

.gsd-input:focus {
  border-color: var(--color-brass);
}

.gsd-input--narrow {
  width: 90px;
}

.gsd-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 2px;
}

.gsd-save-btn {
  font-size: 7px;
  padding: 5px 9px;
}

.gsd-save-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.gsd-error {
  font-size: 10px;
  color: var(--color-red);
  word-break: break-word;
}

.gsd-readonly {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.gsd-ro-field {
  display: flex;
  gap: 8px;
  align-items: baseline;
}

.gsd-ro-label {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-brass-dark);
  letter-spacing: 1px;
  text-transform: uppercase;
  flex: 0 0 44px;
}

.gsd-ro-value {
  font-family: var(--font-mono, monospace);
  font-size: 10px;
  color: var(--color-text);
  word-break: break-word;
}
</style>
