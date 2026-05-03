<template>
  <div class="spawn-form">
    <label class="spawn-label">Repos</label>
    <div v-if="loadingRepos" class="spawn-hint-text">Loading repos…</div>
    <div v-else-if="ghStore.repos.length === 0" class="spawn-hint-text">
      No repos found — configure GitHub first.
    </div>
    <div v-else class="spawn-repo-list">
      <label
        v-for="repo in ghStore.repos"
        :key="repo.full_name"
        class="spawn-repo-row"
        :class="{ selected: selectedRepos.includes(repo.full_name) }"
      >
        <input
          type="checkbox"
          :checked="selectedRepos.includes(repo.full_name)"
          @change="toggleRepo(repo.full_name)"
          class="spawn-repo-check"
        />
        <span class="spawn-repo-name">{{ repo.full_name }}</span>
      </label>
    </div>
    <label class="spawn-label">Name <span class="spawn-hint">(optional)</span></label>
    <input v-model="name" class="spawn-input" type="text" placeholder="auto-generated" />
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
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useGuildStore } from '../../stores/guild'
import { useGitHubStore } from '../../stores/github'
import { api } from '../../utils/api'

const emit = defineEmits<{ (e: 'launched'): void }>()

const guildStore = useGuildStore()
const ghStore = useGitHubStore()

const selectedRepos = ref<string[]>([])
const name = ref('')
const spawning = ref(false)
const error = ref('')
const loadingRepos = ref(false)

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

async function launch() {
  const guild = guildStore.currentGuild
  if (!guild || selectedRepos.value.length === 0) return
  spawning.value = true
  error.value = ''
  try {
    await api(`/guilds/${guild.id}/spawn-worker`, {
      method: 'POST',
      json: {
        repos: selectedRepos.value,
        name: name.value.trim() || undefined,
      },
    })
    emit('launched')
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
