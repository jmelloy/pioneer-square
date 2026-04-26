<template>
  <div class="modal-overlay" @click.self="$emit('close')">
    <div class="modal">
      <div class="modal-header">
        <span class="modal-title">⚙ GITHUB CONFIGURATION</span>
        <button class="close-btn" @click="$emit('close')">✕</button>
      </div>

      <div class="modal-body">

        <!-- Logged in state -->
        <template v-if="ghStore.isConfigured">
          <div class="user-card">
            <img :src="ghStore.user.avatar_url" class="avatar" alt="avatar" />
            <div class="user-info">
              <div class="user-name">{{ ghStore.user.login }}</div>
              <div class="user-label">{{ ghStore.user.name || 'GitHub User' }}</div>
            </div>
            <button class="pixel-btn logout-btn" @click="logout">Disconnect</button>
          </div>

          <div class="divider"></div>

          <div class="repos-section">
            <div class="section-label">WATCHED REPOSITORIES</div>
            <div class="repo-hint">Select repos for agents to work on and issue tracking.</div>

            <div v-if="loadingRepos" class="loading-row">Loading repos...</div>

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
          </div>
        </template>

        <!-- Not logged in state -->
        <template v-else>
          <div class="auth-section">
            <div class="section-label">PERSONAL ACCESS TOKEN</div>
            <div class="token-hint">
              Create a token at github.com/settings/tokens with
              <code>repo</code>, <code>read:org</code>, and <code>read:project</code> scopes.
            </div>
            <input
              v-model="tokenInput"
              type="password"
              class="field-input"
              placeholder="ghp_xxxxxxxxxxxxxxxxxxxx"
              @keydown.enter="connect"
            />
            <div v-if="ghStore.error" class="error-msg">{{ ghStore.error }}</div>
          </div>
        </template>
      </div>

      <div class="modal-footer">
        <template v-if="ghStore.isConfigured">
          <div class="selected-count">
            {{ selectedCount }} repo{{ selectedCount !== 1 ? 's' : '' }} selected
          </div>
          <button class="pixel-btn" @click="save">SAVE</button>
        </template>
        <template v-else>
          <button class="pixel-btn cancel-btn" @click="$emit('close')">Cancel</button>
          <button
            class="pixel-btn connect-btn"
            @click="connect"
            :disabled="ghStore.loading || !tokenInput.trim()"
          >
            {{ ghStore.loading ? 'Connecting...' : 'CONNECT' }}
          </button>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useGitHubStore } from '../stores/github.js'

const emit = defineEmits(['close'])
const ghStore = useGitHubStore()

const tokenInput = ref('')
const loadingRepos = ref(false)
const localSelected = ref([...ghStore.selectedRepos])

const selectedCount = computed(() => localSelected.value.length)

onMounted(async () => {
  if (ghStore.isConfigured && ghStore.repos.length === 0) {
    loadingRepos.value = true
    await ghStore.fetchRepos()
    loadingRepos.value = false
  }
})

function isSelected(fullName) {
  return localSelected.value.includes(fullName)
}

function toggleRepo(fullName) {
  const idx = localSelected.value.indexOf(fullName)
  if (idx >= 0) {
    localSelected.value.splice(idx, 1)
  } else {
    localSelected.value.push(fullName)
  }
}

async function connect() {
  if (!tokenInput.value.trim() || ghStore.loading) return
  const result = await ghStore.authenticate(tokenInput.value.trim())
  if (result.success) {
    tokenInput.value = ''
    loadingRepos.value = true
    await ghStore.fetchRepos()
    loadingRepos.value = false
  }
}

async function save() {
  ghStore.setSelectedRepos(localSelected.value)
  if (localSelected.value.length > 0) {
    await ghStore.fetchIssues()
  }
  emit('close')
}

async function logout() {
  ghStore.logout()
  tokenInput.value = ''
  localSelected.value = []
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
  width: 480px;
  max-height: 80vh;
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
  transition: color 0.15s;
}

.close-btn:hover {
  color: var(--color-text);
}

.modal-body {
  flex: 1;
  overflow-y: auto;
  padding: 18px 16px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* ── User card ── */
.user-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px;
  background: rgba(0, 187, 170, 0.08);
  border: 1px solid rgba(0, 187, 170, 0.3);
  border-left: 3px solid var(--color-teal);
}

.avatar {
  width: 44px;
  height: 44px;
  border-radius: 50%;
  border: 2px solid var(--color-teal);
}

.user-info {
  flex: 1;
}

.user-name {
  font-family: var(--font-pixel);
  font-size: 8px;
  color: var(--color-teal);
  margin-bottom: 4px;
}

.user-label {
  font-size: 11px;
  color: var(--color-text-dim);
}

.logout-btn {
  font-size: 7px;
  padding: 5px 8px;
  background: transparent;
  border-color: var(--color-text-dim);
  color: var(--color-text-dim);
}

.logout-btn:hover {
  border-color: var(--color-red);
  color: var(--color-red);
  box-shadow: none;
}

/* ── Divider ── */
.divider {
  height: 1px;
  background: var(--color-brass-dark);
  opacity: 0.5;
}

/* ── Section ── */
.section-label {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-brass);
  letter-spacing: 2px;
  margin-bottom: 6px;
}

/* ── Auth section ── */
.auth-section {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.token-hint {
  font-size: 11px;
  color: var(--color-text-dim);
  line-height: 1.5;
}

.token-hint code {
  background: var(--color-bg-tertiary);
  border: 1px solid var(--color-brass-dark);
  padding: 1px 5px;
  font-family: var(--font-mono);
  color: var(--color-brass-light);
  font-size: 10px;
}

.field-input {
  background: var(--color-bg);
  border: 2px solid var(--color-brass-dark);
  color: var(--color-text);
  font-family: var(--font-mono);
  font-size: 13px;
  padding: 8px 10px;
  outline: none;
  width: 100%;
}

.field-input:focus {
  border-color: var(--color-brass);
  box-shadow: 0 0 8px rgba(232, 170, 0, 0.3);
}

.field-input::placeholder {
  color: var(--color-text-dim);
}

.error-msg {
  font-size: 11px;
  color: var(--color-red);
  padding: 6px 8px;
  background: rgba(238, 51, 34, 0.1);
  border: 1px solid rgba(238, 51, 34, 0.3);
}

/* ── Repos section ── */
.repos-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.repo-hint {
  font-size: 11px;
  color: var(--color-text-dim);
}

.loading-row {
  font-size: 11px;
  color: var(--color-text-dim);
  padding: 8px 0;
}

.repo-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 280px;
  overflow-y: auto;
}

.repo-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 10px;
  cursor: pointer;
  transition: background 0.1s;
  border: 1px solid transparent;
}

.repo-row:hover {
  background: var(--color-bg-tertiary);
}

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

/* ── Footer ── */
.modal-footer {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
  padding: 12px 16px;
  border-top: 2px solid var(--color-brass-dark);
  background: var(--color-bg-tertiary);
  flex-shrink: 0;
}

.selected-count {
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

.connect-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
  pointer-events: none;
}
</style>
