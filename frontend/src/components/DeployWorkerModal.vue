<template>
  <div class="modal-overlay" @click.self="$emit('close')">
    <div class="modal">
      <div class="modal-header">
        <span class="modal-title">⚙ DEPLOY WORKER</span>
        <button class="close-btn" @click="$emit('close')">✕</button>
      </div>

      <div class="modal-body">
        <div v-if="!authStore.isLoggedIn" class="warn-block">
          Sign in with GitHub first to enable repo tracking and PR creation.
        </div>

        <div class="section-label">REPOSITORIES TO WATCH</div>
        <div class="repo-hint">The worker will clone these repos, pull periodically, and create worktrees when assigned a task.</div>

        <div v-if="ghStore.repos.length === 0" class="no-repos">
          <span v-if="ghStore.isConfigured">No repos loaded — open GitHub config and save.</span>
          <span v-else>Connect GitHub to select repos.</span>
        </div>

        <div v-else class="repo-list">
          <label
            v-for="repo in ghStore.repos"
            :key="repo.full_name"
            class="repo-row"
            :class="{ selected: isSelected(repo.full_name) }"
          >
            <input
              type="checkbox"
              :checked="isSelected(repo.full_name)"
              @change="toggleRepo(repo.full_name)"
              class="repo-check"
            />
            <span class="repo-name">{{ repo.full_name }}</span>
            <span class="repo-lang" v-if="repo.language">{{ repo.language }}</span>
          </label>
        </div>

        <div v-if="selectedRepos.length === 0 && ghStore.selectedRepos.length > 0" class="pre-select-hint">
          <button class="pixel-btn sm-btn" @click="selectedRepos = [...ghStore.selectedRepos]">
            Use GitHub config selection ({{ ghStore.selectedRepos.length }})
          </button>
        </div>

        <div v-if="error" class="error-msg">{{ error }}</div>
      </div>

      <div class="modal-footer">
        <div class="footer-info">{{ selectedRepos.length }} repo{{ selectedRepos.length !== 1 ? 's' : '' }} selected</div>
        <button class="pixel-btn cancel-btn" @click="$emit('close')">Cancel</button>
        <button
          class="pixel-btn deploy-btn"
          @click="deploy"
          :disabled="deploying || selectedRepos.length === 0"
        >
          {{ deploying ? 'Deploying...' : 'DEPLOY' }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useGitHubStore } from '../stores/github.js'
import { useAuthStore } from '../stores/auth.js'
import { useAgentsStore } from '../stores/agents.js'
import { useGuildStore } from '../stores/guild.js'

const emit = defineEmits(['close', 'deployed'])

const ghStore = useGitHubStore()
const authStore = useAuthStore()
const agentsStore = useAgentsStore()
const guildStore = useGuildStore()

const selectedRepos = ref([...ghStore.selectedRepos])
const deploying = ref(false)
const error = ref('')

function isSelected(fullName) {
  return selectedRepos.value.includes(fullName)
}

function toggleRepo(fullName) {
  const idx = selectedRepos.value.indexOf(fullName)
  if (idx >= 0) selectedRepos.value.splice(idx, 1)
  else selectedRepos.value.push(fullName)
}

async function deploy() {
  if (deploying.value || selectedRepos.value.length === 0) return
  deploying.value = true
  error.value = ''
  try {
    const worker = await agentsStore.deployWorker({
      repos: selectedRepos.value,
    })
    emit('deployed', worker)
    emit('close')
  } catch (e) {
    error.value = e.message
  } finally {
    deploying.value = false
  }
}
</script>

<style scoped>
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.75);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 300;
}

.modal {
  background: var(--color-bg-secondary);
  border: 3px solid var(--color-brass);
  box-shadow: 0 0 40px rgba(232, 170, 0, 0.25);
  width: 460px;
  max-height: 75vh;
  display: flex;
  flex-direction: column;
}

.modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  background: var(--color-bg-tertiary);
  border-bottom: 2px solid var(--color-brass-dark);
  flex-shrink: 0;
}

.modal-title {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-brass-light);
  letter-spacing: 2px;
}

.close-btn {
  background: none;
  border: none;
  color: var(--color-text-dim);
  cursor: pointer;
  font-size: 14px;
  padding: 2px 6px;
}

.close-btn:hover { color: var(--color-text); }

.modal-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.warn-block {
  font-size: 11px;
  color: var(--color-amber);
  padding: 8px 10px;
  background: rgba(255, 204, 0, 0.08);
  border: 1px solid rgba(255, 204, 0, 0.3);
  border-left: 3px solid var(--color-amber);
}

.section-label {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-brass);
  letter-spacing: 2px;
}

.repo-hint {
  font-size: 11px;
  color: var(--color-text-dim);
}

.no-repos {
  font-size: 11px;
  color: var(--color-text-dim);
  padding: 12px 0;
}

.repo-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 260px;
  overflow-y: auto;
}

.repo-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 10px;
  cursor: pointer;
  border: 1px solid transparent;
  transition: background 0.1s;
}

.repo-row:hover { background: var(--color-bg-tertiary); }

.repo-row.selected {
  background: rgba(232, 170, 0, 0.08);
  border-color: var(--color-brass-dark);
}

.repo-check {
  accent-color: var(--color-brass);
  width: 14px;
  height: 14px;
  cursor: pointer;
}

.repo-name {
  flex: 1;
  font-size: 12px;
  color: var(--color-text);
}

.repo-lang {
  font-size: 10px;
  color: var(--color-text-dim);
  background: var(--color-bg-tertiary);
  padding: 2px 6px;
  border: 1px solid var(--color-brass-dark);
}

.pre-select-hint {
  padding: 4px 0;
}

.sm-btn {
  font-size: 7px;
  padding: 5px 8px;
}

.error-msg {
  font-size: 11px;
  color: var(--color-red);
  padding: 6px 8px;
  background: rgba(238, 51, 34, 0.1);
  border: 1px solid rgba(238, 51, 34, 0.3);
}

.modal-footer {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  border-top: 2px solid var(--color-brass-dark);
  background: var(--color-bg-tertiary);
  flex-shrink: 0;
}

.footer-info {
  flex: 1;
  font-size: 11px;
  color: var(--color-text-dim);
}

.cancel-btn {
  background: transparent;
  border-color: var(--color-brass-dark);
  color: var(--color-text-dim);
}

.cancel-btn:hover {
  background: rgba(255, 255, 255, 0.05);
  box-shadow: none;
}

.deploy-btn:disabled {
  opacity: 0.4;
  pointer-events: none;
}
</style>
