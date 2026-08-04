<template>
  <div class="gsp">
    <div v-if="loading" class="gsp-hint">Loading…</div>
    <template v-else>
      <p class="gsp-intro">
        Named presets a worker spawn can use instead of specifying tool/provider/repos each time
        (see the <code>profile</code> field on Spawn Worker). Built-in profiles ship with the server
        and can't be edited or deleted.
      </p>

      <table class="gsp-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Tool</th>
            <th>Provider</th>
            <th>Agents</th>
            <th>Repos</th>
            <th>Scope</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in profiles" :key="p.id">
            <td class="gsp-name-cell">
              <span class="gsp-name">{{ p.name }}</span>
              <span v-if="p.description" class="gsp-desc">{{ p.description }}</span>
            </td>
            <td>{{ p.tool }}</td>
            <td>{{ p.provider || '—' }}</td>
            <td>{{ p.default_agent_count ?? '—' }}</td>
            <td :title="p.default_repos.join(', ')">{{ truncatedRepos(p) }}</td>
            <td>
              <span class="gsp-badge" :class="'gsp-badge--' + (p.builtin ? 'builtin' : p.scope)">
                {{ p.builtin ? 'built-in' : p.scope }}
              </span>
            </td>
            <td class="gsp-row-actions">
              <template v-if="!p.builtin && canEdit(p)">
                <button class="gsp-icon-btn" title="Edit profile" @click="startEdit(p)">✎</button>
                <button
                  class="gsp-icon-btn gsp-icon-btn--danger"
                  title="Delete profile"
                  @click="remove(p)"
                >
                  ✕
                </button>
              </template>
            </td>
          </tr>
          <tr v-if="profiles.length === 0">
            <td colspan="7" class="gsp-hint">No spawn profiles yet.</td>
          </tr>
        </tbody>
      </table>

      <button v-if="!showForm" class="pixel-btn gsp-add-btn" @click="startCreate">
        + Add Profile
      </button>

      <div v-else class="gsp-form">
        <div class="gsp-form-row">
          <div class="gsp-field">
            <label class="gsp-field-label">Name</label>
            <input
              v-model="formName"
              class="gsp-input"
              placeholder="e.g. claude-aws"
              spellcheck="false"
              autocomplete="off"
            />
          </div>
          <div class="gsp-field">
            <label class="gsp-field-label">Tool</label>
            <select v-model="formTool" class="gsp-input">
              <option v-for="t in AVAILABLE_TOOLS" :key="t" :value="t">{{ t }}</option>
            </select>
          </div>
        </div>

        <div class="gsp-field">
          <label class="gsp-field-label">Description</label>
          <input
            v-model="formDescription"
            class="gsp-input"
            placeholder="optional"
            spellcheck="false"
          />
        </div>

        <div class="gsp-form-row">
          <div class="gsp-field">
            <label class="gsp-field-label">Provider</label>
            <input
              v-model="formProvider"
              class="gsp-input"
              placeholder="e.g. aws-bedrock"
              spellcheck="false"
              autocomplete="off"
            />
          </div>
          <div class="gsp-field">
            <label class="gsp-field-label">Credentials Source</label>
            <input
              v-model="formCredentialsSource"
              class="gsp-input"
              placeholder="e.g. aws_bedrock"
              spellcheck="false"
              autocomplete="off"
            />
          </div>
          <div class="gsp-field gsp-field--narrow">
            <label class="gsp-field-label">Agents</label>
            <input
              v-model.number="formAgentCount"
              class="gsp-input"
              type="number"
              min="1"
              max="16"
              placeholder="—"
            />
          </div>
        </div>

        <div class="gsp-field">
          <label class="gsp-field-label">Repos</label>
          <input
            v-model="formRepos"
            class="gsp-input"
            placeholder="owner/repo, owner/other-repo"
            spellcheck="false"
            autocomplete="off"
          />
        </div>

        <div v-if="canManageGuildScope && !editingId" class="gsp-field">
          <label class="gsp-field-label">Scope</label>
          <select v-model="formScope" class="gsp-input">
            <option value="guild">Guild-wide</option>
            <option value="user">Only me</option>
          </select>
        </div>

        <div class="gsp-form-actions">
          <button class="pixel-btn gsp-save-btn" :disabled="saving" @click="save">
            {{ saving ? 'Saving…' : editingId ? 'Save' : 'Create' }}
          </button>
          <button class="gsp-cancel-btn" :disabled="saving" @click="cancelForm">Cancel</button>
        </div>
        <div v-if="formError" class="gsp-error">{{ formError }}</div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { api, ApiError } from '../utils/api'
import { useAuthStore } from '../stores/auth'

const AVAILABLE_TOOLS = ['claude', 'codex', 'pi'] as const

interface SpawnProfile {
  id: string
  name: string
  description: string | null
  tool: string
  provider: string | null
  credentials_source: string | null
  default_agent_count: number | null
  default_repos: string[]
  scope: 'guild' | 'user'
  builtin: boolean
}

interface GuildMemberRow {
  user_id: string
  role: string
}

const props = defineProps<{ guildId: string }>()

const authStore = useAuthStore()

const loading = ref(true)
const profiles = ref<SpawnProfile[]>([])
const myRole = ref<string | null>(null)
const canManageGuildScope = computed(() => myRole.value === 'owner')

const showForm = ref(false)
const editingId = ref<string | null>(null)
const saving = ref(false)
const formError = ref('')

const formName = ref('')
const formDescription = ref('')
const formTool = ref<(typeof AVAILABLE_TOOLS)[number]>('claude')
const formProvider = ref('')
const formCredentialsSource = ref('')
const formAgentCount = ref<number | ''>('')
const formRepos = ref('')
const formScope = ref<'guild' | 'user'>('guild')

onMounted(async () => {
  await Promise.all([loadRole(), loadProfiles()])
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

async function loadProfiles() {
  try {
    profiles.value = await api<SpawnProfile[]>(
      `/guilds/${encodeURIComponent(props.guildId)}/spawn-profiles`,
    )
  } catch {
    profiles.value = []
  }
}

function truncatedRepos(p: SpawnProfile): string {
  if (!p.default_repos.length) return '—'
  const joined = p.default_repos.join(', ')
  return joined.length > 40 ? joined.slice(0, 37) + '…' : joined
}

// The list already only carries built-ins, this guild's rows, and the caller's
// own user-scoped rows — so any non-builtin row present belongs to either the
// guild (needs owner) or the caller.
function canEdit(p: SpawnProfile): boolean {
  if (p.scope === 'guild') return canManageGuildScope.value
  return true
}

function resetForm() {
  formName.value = ''
  formDescription.value = ''
  formTool.value = 'claude'
  formProvider.value = ''
  formCredentialsSource.value = ''
  formAgentCount.value = ''
  formRepos.value = ''
  formScope.value = canManageGuildScope.value ? 'guild' : 'user'
  formError.value = ''
}

function startCreate() {
  editingId.value = null
  resetForm()
  showForm.value = true
}

function startEdit(p: SpawnProfile) {
  editingId.value = p.id
  formName.value = p.name
  formDescription.value = p.description ?? ''
  formTool.value = (p.tool as (typeof AVAILABLE_TOOLS)[number]) ?? 'claude'
  formProvider.value = p.provider ?? ''
  formCredentialsSource.value = p.credentials_source ?? ''
  formAgentCount.value = p.default_agent_count ?? ''
  formRepos.value = p.default_repos.join(', ')
  formScope.value = p.scope
  formError.value = ''
  showForm.value = true
}

function cancelForm() {
  showForm.value = false
  editingId.value = null
  formError.value = ''
}

function parseRepos(): string[] {
  return formRepos.value
    .split(',')
    .map((r) => r.trim())
    .filter(Boolean)
}

function upsertLocal(row: SpawnProfile) {
  const idx = profiles.value.findIndex((p) => p.id === row.id)
  if (idx >= 0) profiles.value[idx] = row
  else profiles.value.push(row)
  profiles.value.sort((a, b) => a.name.localeCompare(b.name))
}

async function save() {
  const name = formName.value.trim()
  if (!name) {
    formError.value = 'Name is required'
    return
  }
  if (
    formAgentCount.value !== '' &&
    (!Number.isInteger(formAgentCount.value) ||
      (formAgentCount.value as number) < 1 ||
      (formAgentCount.value as number) > 16)
  ) {
    formError.value = 'Agent count must be a whole number between 1 and 16'
    return
  }
  saving.value = true
  formError.value = ''
  const payload = {
    name,
    description: formDescription.value.trim() || null,
    tool: formTool.value,
    provider: formProvider.value.trim() || null,
    credentials_source: formCredentialsSource.value.trim() || null,
    default_agent_count: formAgentCount.value === '' ? null : formAgentCount.value,
    default_repos: parseRepos(),
  }
  try {
    if (editingId.value) {
      const updated = await api<SpawnProfile>(
        `/guilds/${encodeURIComponent(props.guildId)}/spawn-profiles/${encodeURIComponent(editingId.value)}`,
        { method: 'PATCH', json: payload },
      )
      upsertLocal(updated)
    } else {
      const created = await api<SpawnProfile>(
        `/guilds/${encodeURIComponent(props.guildId)}/spawn-profiles`,
        { method: 'POST', json: { ...payload, scope: formScope.value } },
      )
      upsertLocal(created)
    }
    showForm.value = false
    editingId.value = null
  } catch (e) {
    formError.value = e instanceof ApiError ? e.message : 'Failed to save profile'
  } finally {
    saving.value = false
  }
}

async function remove(p: SpawnProfile) {
  if (!window.confirm(`Delete spawn profile "${p.name}"? This cannot be undone.`)) return
  formError.value = ''
  try {
    await api(
      `/guilds/${encodeURIComponent(props.guildId)}/spawn-profiles/${encodeURIComponent(p.id)}`,
      { method: 'DELETE' },
    )
    profiles.value = profiles.value.filter((x) => x.id !== p.id)
  } catch (e) {
    formError.value = e instanceof ApiError ? e.message : 'Failed to delete profile'
  }
}
</script>

<style scoped>
.gsp {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 6px;
  padding: 10px;
  background: var(--color-bg);
  border: 1px solid var(--color-brass-dark);
  border-radius: 2px;
}

.gsp-intro {
  font-size: 9px;
  color: var(--color-text-dim);
  font-style: italic;
  margin: 0;
  line-height: 1.4;
}

.gsp-hint {
  font-size: 10px;
  color: var(--color-text-dim);
  font-style: italic;
}

.gsp-table {
  width: 100%;
  border-collapse: collapse;
  font-family: var(--font-mono, monospace);
  font-size: 10px;
}

.gsp-table th {
  text-align: left;
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-brass-dark);
  letter-spacing: 1px;
  text-transform: uppercase;
  padding: 4px 6px;
  border-bottom: 1px solid var(--color-brass-dark);
}

.gsp-table td {
  padding: 5px 6px;
  border-bottom: 1px solid var(--color-bg-tertiary);
  color: var(--color-text);
  vertical-align: top;
}

.gsp-name-cell {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.gsp-name {
  color: var(--color-brass-light);
}

.gsp-desc {
  font-size: 9px;
  color: var(--color-text-dim);
  font-style: italic;
}

.gsp-badge {
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  padding: 3px 6px;
  border: 1px solid var(--color-brass-dark);
  border-radius: 2px;
  color: var(--color-text-dim);
  white-space: nowrap;
}

.gsp-badge--builtin {
  color: var(--color-teal);
  border-color: var(--color-teal);
}

.gsp-badge--guild {
  color: var(--color-brass-light);
  border-color: var(--color-brass);
}

.gsp-row-actions {
  display: flex;
  gap: 4px;
  white-space: nowrap;
}

.gsp-icon-btn {
  background: none;
  border: 1px solid var(--color-brass-dark);
  color: var(--color-text-dim);
  cursor: pointer;
  width: 20px;
  height: 20px;
  border-radius: 2px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  flex-shrink: 0;
  transition:
    border-color 0.12s,
    color 0.12s;
}

.gsp-icon-btn:hover {
  border-color: var(--color-brass);
  color: var(--color-brass);
}

.gsp-icon-btn--danger:hover {
  border-color: var(--color-red, #c0392b);
  color: var(--color-red, #c0392b);
}

.gsp-add-btn {
  font-size: 7px;
  padding: 5px 9px;
  align-self: flex-start;
}

.gsp-form {
  display: flex;
  flex-direction: column;
  gap: 8px;
  border-top: 1px solid var(--color-brass-dark);
  padding-top: 10px;
  margin-top: 2px;
}

.gsp-form-row {
  display: flex;
  gap: 8px;
}

.gsp-field {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.gsp-field--narrow {
  flex: 0 0 80px;
}

.gsp-field-label {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-brass-dark);
  letter-spacing: 1px;
  text-transform: uppercase;
}

.gsp-input {
  background: var(--color-bg-secondary);
  border: 1px solid var(--color-brass-dark);
  color: var(--color-text);
  font-family: var(--font-mono, monospace);
  font-size: 11px;
  padding: 5px 7px;
  outline: none;
  border-radius: 2px;
  box-sizing: border-box;
  width: 100%;
}

.gsp-input:focus {
  border-color: var(--color-brass);
}

.gsp-form-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 2px;
}

.gsp-save-btn {
  font-size: 7px;
  padding: 5px 9px;
}

.gsp-save-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.gsp-cancel-btn {
  background: none;
  border: 1px solid var(--color-brass-dark);
  color: var(--color-text-dim);
  cursor: pointer;
  font-family: var(--font-pixel);
  font-size: 7px;
  letter-spacing: 1px;
  text-transform: uppercase;
  padding: 5px 9px;
  border-radius: 2px;
}

.gsp-cancel-btn:hover:not(:disabled) {
  border-color: var(--color-brass);
  color: var(--color-brass);
}

.gsp-error {
  font-size: 10px;
  color: var(--color-red);
  word-break: break-word;
}
</style>
