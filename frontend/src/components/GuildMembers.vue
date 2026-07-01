<template>
  <div class="members-section">
    <div v-if="loading" class="members-loading">Loading…</div>
    <div v-else>
      <ul class="member-list">
        <li v-for="m in members" :key="m.user_id" class="member-row">
          <img v-if="m.avatar_url" :src="m.avatar_url" class="member-avatar" alt="" />
          <span v-else class="member-avatar member-avatar--placeholder">{{ initials(m) }}</span>
          <span class="member-name" :title="m.user_id">{{ memberLabel(m) }}</span>
          <select
            v-if="canManage && !isLastOwner(m)"
            class="member-role-select"
            :value="m.role"
            :disabled="busyUser === m.user_id"
            @change="changeRole(m, ($event.target as HTMLSelectElement).value)"
          >
            <option v-for="r in ROLES" :key="r" :value="r">{{ r }}</option>
          </select>
          <span v-else class="member-role-badge" :class="'member-role-badge--' + m.role">{{
            m.role
          }}</span>
          <button
            v-if="canManage && !isLastOwner(m)"
            class="member-remove-btn"
            :disabled="busyUser === m.user_id"
            title="Remove member"
            @click="removeMember(m)"
          >
            ✕
          </button>
        </li>
      </ul>

      <div v-if="canManage && pendingInvites.length" class="pending-invites">
        <div class="pending-invites-label">Pending invites</div>
        <ul class="member-list">
          <li v-for="inv in pendingInvites" :key="inv.id" class="member-row">
            <span class="member-avatar member-avatar--placeholder">?</span>
            <span class="member-name">{{ inv.github_login || inv.github_id }}</span>
            <span class="member-role-badge">{{ inv.role }}</span>
            <button
              class="member-remove-btn"
              :disabled="busyInvite === inv.id"
              title="Cancel invite"
              @click="cancelInvite(inv)"
            >
              ✕
            </button>
          </li>
        </ul>
      </div>

      <div v-if="canManage" class="invite-form">
        <div class="invite-row">
          <input
            v-model="inviteValue"
            class="settings-input invite-input"
            placeholder="GitHub username or ID"
            spellcheck="false"
            autocomplete="off"
            :disabled="inviting"
            @keydown.enter="invite"
          />
          <select v-model="inviteRole" class="member-role-select" :disabled="inviting">
            <option v-for="r in ROLES" :key="r" :value="r">{{ r }}</option>
          </select>
          <button
            class="pixel-btn invite-btn"
            :disabled="inviting || !inviteValue.trim()"
            @click="invite"
          >
            {{ inviting ? '…' : 'Invite' }}
          </button>
        </div>
        <div v-if="inviteError" class="members-error">{{ inviteError }}</div>
        <div v-else-if="inviteHint" class="members-hint">{{ inviteHint }}</div>
      </div>
      <div v-else-if="!loading" class="members-hint">Only owners can invite members.</div>

      <div v-if="loadError" class="members-error">{{ loadError }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { api, ApiError } from '../utils/api'
import { useAuthStore } from '../stores/auth'

const ROLES = ['owner', 'member', 'viewer'] as const

interface GuildMemberRow {
  user_id: string
  role: string
  created_at: string
  github_login: string | null
  display_name: string | null
  avatar_url: string | null
}

interface GuildInviteRow {
  id: number
  github_login: string | null
  github_id: string | null
  role: string
  created_at: string
  status: string
}

const props = defineProps<{ guildId: string }>()

const authStore = useAuthStore()

const members = ref<GuildMemberRow[]>([])
const loading = ref(false)
const loadError = ref('')

const pendingInvites = ref<GuildInviteRow[]>([])
const busyInvite = ref<number | null>(null)

const inviteValue = ref('')
const inviteRole = ref<string>('member')
const inviting = ref(false)
const inviteError = ref('')
const inviteHint = ref('')

const busyUser = ref<string | null>(null)

// The caller's role in this guild, derived from the members list.
const myRole = computed(() => {
  const id = authStore.user?.id
  if (!id) return null
  return members.value.find((m) => m.user_id === id)?.role ?? null
})
const canManage = computed(() => myRole.value === 'owner')

const ownerCount = computed(() => members.value.filter((m) => m.role === 'owner').length)

// The last remaining owner can't be demoted or removed (backend enforces this
// too); hide the controls so the UI matches.
function isLastOwner(m: GuildMemberRow): boolean {
  return m.role === 'owner' && ownerCount.value <= 1
}

function memberLabel(m: GuildMemberRow): string {
  return m.github_login || m.display_name || m.user_id
}

function initials(m: GuildMemberRow): string {
  const label = memberLabel(m)
  return label.slice(0, 2).toUpperCase()
}

async function load() {
  if (!props.guildId) return
  loading.value = true
  loadError.value = ''
  try {
    members.value = await api<GuildMemberRow[]>(
      `/api/guilds/${encodeURIComponent(props.guildId)}/members`,
    )
  } catch (e) {
    loadError.value = e instanceof ApiError ? e.message : 'Failed to load members'
    members.value = []
  } finally {
    loading.value = false
  }
  await loadInvites()
}

async function loadInvites() {
  if (!canManage.value) return
  try {
    pendingInvites.value = await api<GuildInviteRow[]>(
      `/api/guilds/${encodeURIComponent(props.guildId)}/invites`,
    )
  } catch {
    pendingInvites.value = []
  }
}

async function invite() {
  const user = inviteValue.value.trim()
  if (!user || inviting.value) return
  inviting.value = true
  inviteError.value = ''
  inviteHint.value = ''
  try {
    // Try direct add first (user has already logged in).
    await api(`/api/guilds/${encodeURIComponent(props.guildId)}/members`, {
      method: 'POST',
      json: { user, role: inviteRole.value },
    })
    inviteValue.value = ''
    inviteHint.value = `Added ${user} as ${inviteRole.value}.`
    await load()
  } catch (e) {
    // Only fall back to a pending invite when the server says the user hasn't
    // logged in yet. Any other 404 (guild not found, gateway misconfiguration,
    // etc.) should surface as a real error so the problem isn't silently eaten.
    const isUserNotFound =
      e instanceof ApiError && e.status === 404 && e.message.startsWith("User '")
    if (isUserNotFound) {
      try {
        await api(`/api/guilds/${encodeURIComponent(props.guildId)}/invites`, {
          method: 'POST',
          json: { user, role: inviteRole.value },
        })
        inviteValue.value = ''
        inviteHint.value = `Invite sent to ${user} — they'll be added when they first log in.`
        await load()
      } catch (e2) {
        inviteError.value = e2 instanceof ApiError ? e2.message : 'Failed to invite member'
      }
    } else {
      inviteError.value = e instanceof ApiError ? e.message : 'Failed to invite member'
    }
  } finally {
    inviting.value = false
  }
}

async function cancelInvite(inv: GuildInviteRow) {
  busyInvite.value = inv.id
  inviteError.value = ''
  try {
    await api(`/api/guilds/${encodeURIComponent(props.guildId)}/invites/${inv.id}`, {
      method: 'DELETE',
    })
    await loadInvites()
  } catch (e) {
    inviteError.value = e instanceof ApiError ? e.message : 'Failed to cancel invite'
  } finally {
    busyInvite.value = null
  }
}

async function changeRole(m: GuildMemberRow, role: string) {
  if (role === m.role) return
  busyUser.value = m.user_id
  inviteError.value = ''
  try {
    await api(`/api/guilds/${encodeURIComponent(props.guildId)}/members/${m.user_id}`, {
      method: 'PATCH',
      json: { role },
    })
    await load()
  } catch (e) {
    inviteError.value = e instanceof ApiError ? e.message : 'Failed to change role'
    // Reload to revert the optimistic <select> value to the server's state.
    await load()
  } finally {
    busyUser.value = null
  }
}

async function removeMember(m: GuildMemberRow) {
  busyUser.value = m.user_id
  inviteError.value = ''
  try {
    await api(`/api/guilds/${encodeURIComponent(props.guildId)}/members/${m.user_id}`, {
      method: 'DELETE',
    })
    await load()
  } catch (e) {
    inviteError.value = e instanceof ApiError ? e.message : 'Failed to remove member'
  } finally {
    busyUser.value = null
  }
}

watch(() => props.guildId, load, { immediate: true })
</script>

<style scoped>
.members-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 6px;
  padding: 10px;
  background: var(--color-bg);
  border: 1px solid var(--color-brass-dark);
  border-radius: 2px;
}

.members-loading {
  font-family: var(--font-mono, monospace);
  font-size: 10px;
  color: var(--color-text-dim);
}

.member-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.member-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.member-avatar {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  border: 1px solid var(--color-teal);
  flex-shrink: 0;
}

.member-avatar--placeholder {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: var(--color-bg-secondary);
  color: var(--color-text-dim);
  font-family: var(--font-mono, monospace);
  font-size: 8px;
}

.member-name {
  flex: 1;
  min-width: 0;
  font-family: var(--font-mono, monospace);
  font-size: 11px;
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.member-role-select {
  background: var(--color-bg);
  border: 1px solid var(--color-brass-dark);
  color: var(--color-text);
  font-family: var(--font-mono, monospace);
  font-size: 10px;
  padding: 3px 5px;
  border-radius: 2px;
  flex-shrink: 0;
}

.member-role-badge {
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  padding: 3px 6px;
  border: 1px solid var(--color-brass-dark);
  border-radius: 2px;
  color: var(--color-text-dim);
  flex-shrink: 0;
}

.member-role-badge--owner {
  color: var(--color-brass-light);
  border-color: var(--color-brass);
}

.member-remove-btn {
  background: none;
  border: 1px solid var(--color-brass-dark);
  color: var(--color-text-dim);
  cursor: pointer;
  width: 22px;
  height: 22px;
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

.member-remove-btn:hover:not(:disabled) {
  border-color: var(--color-red, #c0392b);
  color: var(--color-red, #c0392b);
}

.member-remove-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.pending-invites {
  display: flex;
  flex-direction: column;
  gap: 4px;
  border-top: 1px solid var(--color-brass-dark);
  padding-top: 8px;
  margin-top: 4px;
}

.pending-invites-label {
  font-family: var(--font-pixel);
  font-size: 7px;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  color: var(--color-text-dim);
  margin-bottom: 2px;
}

.invite-form {
  display: flex;
  flex-direction: column;
  gap: 4px;
  border-top: 1px solid var(--color-brass-dark);
  padding-top: 8px;
}

.invite-row {
  display: flex;
  align-items: center;
  gap: 4px;
}

.invite-input {
  flex: 1;
  font-size: 10px;
}

.invite-btn {
  font-size: 7px;
  padding: 5px 9px;
  flex-shrink: 0;
}

.invite-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.members-error {
  font-family: var(--font-mono, monospace);
  font-size: 10px;
  color: var(--color-red, #e74c3c);
}

.members-hint {
  font-family: var(--font-mono, monospace);
  font-size: 10px;
  color: var(--color-text-dim);
}
</style>
