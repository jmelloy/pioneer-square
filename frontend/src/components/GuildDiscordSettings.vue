<template>
  <div class="discord-settings">
    <nav class="foreman-tool-tabs">
      <button
        v-for="t in SUBTABS"
        :key="t.id"
        type="button"
        class="foreman-tool-tab"
        :class="{ active: subTab === t.id }"
        @click="subTab = t.id"
      >
        {{ t.label }}
      </button>
    </nav>

    <!-- Status -->
    <template v-if="subTab === 'status'">
      <div v-if="statusLoading" class="discord-loading">Loading…</div>
      <div v-else-if="statusError" class="discord-error">{{ statusError }}</div>
      <template v-else-if="status">
        <p class="foreman-hint">
          Pioneer Square runs one Discord bot per instance, configured via server env vars — not per
          guild. This reflects the live server configuration and Gateway connection state.
        </p>
        <div class="discord-status-grid">
          <div class="discord-status-row">
            <span class="discord-status-label">Bot configured</span>
            <span class="discord-status-value">
              <span
                class="status-dot"
                :class="status.bot_configured ? 'status-dot--ok' : 'status-dot--off'"
              />
              {{ status.bot_configured ? 'Yes' : 'No' }}
            </span>
          </div>
          <div class="discord-status-row">
            <span class="discord-status-label">Bot token</span>
            <span class="discord-status-value">{{ status.bot_token_set ? 'Set' : 'Not set' }}</span>
          </div>
          <div class="discord-status-row">
            <span class="discord-status-label">Channel ID</span>
            <span class="discord-status-value">{{
              status.channel_id_set ? 'Set' : 'Not set'
            }}</span>
          </div>
          <div class="discord-status-row">
            <span class="discord-status-label">Slash commands</span>
            <span class="discord-status-value">
              <span
                class="status-dot"
                :class="status.slash_commands_configured ? 'status-dot--ok' : 'status-dot--off'"
              />
              {{ status.slash_commands_configured ? 'Configured' : 'Not configured' }}
            </span>
          </div>
          <div class="discord-status-row">
            <span class="discord-status-label">Gateway</span>
            <span class="discord-status-value">
              <span
                class="status-dot"
                :class="status.gateway.running ? 'status-dot--ok' : 'status-dot--off'"
              />
              {{
                status.gateway.enabled
                  ? status.gateway.running
                    ? 'Running'
                    : 'Stopped'
                  : 'Disabled'
              }}
            </span>
          </div>
          <div class="discord-status-row">
            <span class="discord-status-label">Task streaming</span>
            <span class="discord-status-value">{{
              status.stream_tasks_enabled ? 'On' : 'Off'
            }}</span>
          </div>
          <div class="discord-status-row">
            <span class="discord-status-label">Operator role</span>
            <span class="discord-status-value">{{ status.operator_role_name }}</span>
          </div>
          <div class="discord-status-row" v-if="status.allowed_role_ids.length">
            <span class="discord-status-label">Allowed roles</span>
            <span class="discord-status-value discord-status-value--mono">{{
              status.allowed_role_ids.join(', ')
            }}</span>
          </div>
          <div class="discord-status-row" v-if="status.pioneer_guild_slug">
            <span class="discord-status-label">Guild slug</span>
            <span class="discord-status-value discord-status-value--mono">{{
              status.pioneer_guild_slug
            }}</span>
          </div>
        </div>
      </template>
    </template>

    <!-- Accounts -->
    <template v-else-if="subTab === 'accounts'">
      <p class="foreman-hint">
        Discord accounts linked to human members of this guild via <code>/connect-account</code>.
      </p>
      <div v-if="accountsLoading" class="discord-loading">Loading…</div>
      <div v-else-if="accountsError" class="discord-error">{{ accountsError }}</div>
      <template v-else>
        <ul v-if="accounts.length" class="discord-list">
          <li v-for="a in accounts" :key="a.discord_user_id" class="discord-row">
            <img v-if="a.avatar_url" :src="a.avatar_url" class="discord-avatar" alt="" />
            <span v-else class="discord-avatar discord-avatar--placeholder">{{
              initials(a.github_login || a.display_name || a.discord_username)
            }}</span>
            <div class="discord-row-main">
              <span class="discord-row-title">{{
                a.github_login || a.display_name || a.user_id
              }}</span>
              <span class="discord-row-sub"
                >@{{ a.discord_username }} · linked {{ formatRelative(a.linked_at) }}</span
              >
            </div>
            <button
              v-if="canManage"
              class="discord-remove-btn"
              :disabled="unlinkBusy === a.discord_user_id"
              title="Unlink this Discord account"
              @click="unlinkAccount(a)"
            >
              ✕
            </button>
          </li>
        </ul>
        <p v-else class="discord-empty">No Discord accounts linked yet.</p>
        <p v-if="accountsActionError" class="discord-error">{{ accountsActionError }}</p>
      </template>
    </template>

    <!-- Bots -->
    <template v-else-if="subTab === 'bots'">
      <p class="foreman-hint">
        Bot identities auto-provisioned the first time another Discord bot posted in a wired channel
        of this guild. Read-only — Pioneer Square's own bot has no per-guild row here.
      </p>
      <div v-if="botsLoading" class="discord-loading">Loading…</div>
      <div v-else-if="botsError" class="discord-error">{{ botsError }}</div>
      <template v-else>
        <ul v-if="bots.length" class="discord-list">
          <li v-for="b in bots" :key="b.user_id" class="discord-row">
            <span class="discord-avatar discord-avatar--placeholder">{{
              initials(b.display_name || b.user_id)
            }}</span>
            <div class="discord-row-main">
              <span class="discord-row-title">{{ b.display_name || b.user_id }}</span>
              <span class="discord-row-sub">
                {{
                  b.discord_channel_id ? `channel ${b.discord_channel_id}` : 'no preferred channel'
                }}
                · seen {{ formatRelative(b.created_at) }}
              </span>
            </div>
            <span class="discord-role-badge" :class="'discord-role-badge--' + b.role">{{
              b.role
            }}</span>
          </li>
        </ul>
        <p v-else class="discord-empty">No Discord bots seen in this guild yet.</p>
      </template>
    </template>

    <!-- Channels -->
    <template v-else-if="subTab === 'channels'">
      <p class="foreman-hint">
        Discord channels wired to this guild for event routing — equivalent to running
        <code>/join-channel</code> in Discord.
      </p>
      <div v-if="channelsLoading" class="discord-loading">Loading…</div>
      <div v-else-if="channelsError" class="discord-error">{{ channelsError }}</div>
      <template v-else>
        <ul v-if="channels.length" class="discord-list">
          <li v-for="c in channels" :key="c.id" class="discord-row">
            <div class="discord-row-main">
              <span class="discord-row-title discord-row-title--mono"
                >guild {{ c.discord_guild_id }} / channel {{ c.discord_channel_id }}</span
              >
              <span class="discord-row-sub">wired {{ formatRelative(c.created_at) }}</span>
            </div>
            <button
              v-if="canManage"
              class="discord-remove-btn"
              :disabled="channelBusy === c.id"
              title="Remove this channel binding"
              @click="removeChannel(c)"
            >
              ✕
            </button>
          </li>
        </ul>
        <p v-else class="discord-empty">No Discord channels wired to this guild yet.</p>

        <div v-if="canManage" class="discord-add-form">
          <div class="discord-add-row">
            <input
              v-model="newChannelGuildId"
              class="settings-input"
              placeholder="Discord guild ID"
              spellcheck="false"
              autocomplete="off"
              :disabled="channelAdding"
            />
            <input
              v-model="newChannelId"
              class="settings-input"
              placeholder="Discord channel ID"
              spellcheck="false"
              autocomplete="off"
              :disabled="channelAdding"
              @keydown.enter="addChannel"
            />
            <button
              class="pixel-btn"
              :disabled="channelAdding || !newChannelGuildId.trim() || !newChannelId.trim()"
              @click="addChannel"
            >
              {{ channelAdding ? '…' : 'Add' }}
            </button>
          </div>
          <p v-if="channelAddError" class="discord-error">{{ channelAddError }}</p>
        </div>
        <p v-else class="discord-empty">Only owners can manage channel bindings.</p>
      </template>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { api, ApiError } from '../utils/api'
import { useAuthStore } from '../stores/auth'
import { formatRelative } from '../utils/format'

interface DiscordStatus {
  bot_configured: boolean
  bot_token_set: boolean
  channel_id_set: boolean
  slash_commands_configured: boolean
  stream_tasks_enabled: boolean
  pioneer_guild_slug: string | null
  allowed_role_ids: string[]
  operator_role_name: string
  gateway: { enabled: boolean; running: boolean }
}

interface DiscordAccountRow {
  user_id: string
  github_login: string | null
  display_name: string | null
  avatar_url: string | null
  discord_user_id: string
  discord_username: string
  linked_at: string
}

interface DiscordBotRow {
  user_id: string
  display_name: string | null
  discord_channel_id: string | null
  parent_user_id: string | null
  created_at: string
  role: string
}

interface DiscordChannelRow {
  id: number
  discord_guild_id: string
  discord_channel_id: string
  ps_guild_id: string
  created_at: string
}

interface GuildMemberRow {
  user_id: string
  role: string
}

const props = defineProps<{ guildId: string }>()

const authStore = useAuthStore()

const SUBTABS = [
  { id: 'status', label: 'Status' },
  { id: 'accounts', label: 'Accounts' },
  { id: 'bots', label: 'Bots' },
  { id: 'channels', label: 'Channels' },
] as const
const subTab = ref<(typeof SUBTABS)[number]['id']>('status')

// Whether the current user can manage owner-only actions (unlink accounts,
// wire/unwire channels) — derived from the guild's member list, same
// approach GuildMembers.vue uses.
const members = ref<GuildMemberRow[]>([])
const canManage = computed(() => {
  const id = authStore.user?.id
  if (!id) return false
  return members.value.find((m) => m.user_id === id)?.role === 'owner'
})

const status = ref<DiscordStatus | null>(null)
const statusLoading = ref(false)
const statusError = ref('')

const accounts = ref<DiscordAccountRow[]>([])
const accountsLoading = ref(false)
const accountsError = ref('')
const accountsActionError = ref('')
const unlinkBusy = ref<string | null>(null)

const bots = ref<DiscordBotRow[]>([])
const botsLoading = ref(false)
const botsError = ref('')

const channels = ref<DiscordChannelRow[]>([])
const channelsLoading = ref(false)
const channelsError = ref('')
const channelBusy = ref<number | null>(null)
const newChannelGuildId = ref('')
const newChannelId = ref('')
const channelAdding = ref(false)
const channelAddError = ref('')

function initials(label: string): string {
  return (label || '?').slice(0, 2).toUpperCase()
}

async function loadMembers() {
  if (!props.guildId) return
  try {
    members.value = await api<GuildMemberRow[]>(
      `/api/guilds/${encodeURIComponent(props.guildId)}/members`,
    )
  } catch {
    members.value = []
  }
}

async function loadStatus() {
  if (!props.guildId) return
  statusLoading.value = true
  statusError.value = ''
  try {
    status.value = await api<DiscordStatus>(
      `/api/guilds/${encodeURIComponent(props.guildId)}/discord/status`,
    )
  } catch (e) {
    statusError.value = e instanceof ApiError ? e.message : 'Failed to load Discord status'
    status.value = null
  } finally {
    statusLoading.value = false
  }
}

async function loadAccounts() {
  if (!props.guildId) return
  accountsLoading.value = true
  accountsError.value = ''
  try {
    accounts.value = await api<DiscordAccountRow[]>(
      `/api/guilds/${encodeURIComponent(props.guildId)}/discord/accounts`,
    )
  } catch (e) {
    accountsError.value = e instanceof ApiError ? e.message : 'Failed to load Discord accounts'
    accounts.value = []
  } finally {
    accountsLoading.value = false
  }
}

async function unlinkAccount(a: DiscordAccountRow) {
  unlinkBusy.value = a.discord_user_id
  accountsActionError.value = ''
  try {
    await api(
      `/api/guilds/${encodeURIComponent(props.guildId)}/discord/accounts/${encodeURIComponent(a.discord_user_id)}`,
      { method: 'DELETE' },
    )
    await loadAccounts()
  } catch (e) {
    accountsActionError.value = e instanceof ApiError ? e.message : 'Failed to unlink account'
  } finally {
    unlinkBusy.value = null
  }
}

async function loadBots() {
  if (!props.guildId) return
  botsLoading.value = true
  botsError.value = ''
  try {
    bots.value = await api<DiscordBotRow[]>(
      `/api/guilds/${encodeURIComponent(props.guildId)}/discord/bots`,
    )
  } catch (e) {
    botsError.value = e instanceof ApiError ? e.message : 'Failed to load Discord bots'
    bots.value = []
  } finally {
    botsLoading.value = false
  }
}

async function loadChannels() {
  if (!props.guildId) return
  channelsLoading.value = true
  channelsError.value = ''
  try {
    channels.value = await api<DiscordChannelRow[]>(
      `/api/guilds/${encodeURIComponent(props.guildId)}/discord/channels`,
    )
  } catch (e) {
    channelsError.value = e instanceof ApiError ? e.message : 'Failed to load Discord channels'
    channels.value = []
  } finally {
    channelsLoading.value = false
  }
}

async function addChannel() {
  const discordGuildId = newChannelGuildId.value.trim()
  const discordChannelId = newChannelId.value.trim()
  if (!discordGuildId || !discordChannelId || channelAdding.value) return
  channelAdding.value = true
  channelAddError.value = ''
  try {
    await api(`/api/guilds/${encodeURIComponent(props.guildId)}/discord/channels`, {
      method: 'POST',
      json: { discord_guild_id: discordGuildId, discord_channel_id: discordChannelId },
    })
    newChannelGuildId.value = ''
    newChannelId.value = ''
    await loadChannels()
  } catch (e) {
    channelAddError.value = e instanceof ApiError ? e.message : 'Failed to add channel binding'
  } finally {
    channelAdding.value = false
  }
}

async function removeChannel(c: DiscordChannelRow) {
  channelBusy.value = c.id
  channelsError.value = ''
  try {
    await api(`/api/guilds/${encodeURIComponent(props.guildId)}/discord/channels/${c.id}`, {
      method: 'DELETE',
    })
    await loadChannels()
  } catch (e) {
    channelsError.value = e instanceof ApiError ? e.message : 'Failed to remove channel binding'
  } finally {
    channelBusy.value = null
  }
}

async function loadAll() {
  await Promise.all([loadMembers(), loadStatus(), loadAccounts(), loadBots(), loadChannels()])
}

watch(() => props.guildId, loadAll, { immediate: true })
</script>

<style scoped>
.discord-settings {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.foreman-tool-tabs {
  display: flex;
  gap: 4px;
  margin-bottom: 2px;
}

.foreman-tool-tab {
  background: none;
  border: 1px solid var(--color-brass-dark);
  color: var(--color-brass-dark);
  cursor: pointer;
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 1px;
  text-transform: uppercase;
  padding: 6px 12px;
  border-radius: 2px;
  transition:
    color 0.12s,
    background 0.12s,
    border-color 0.12s;
}

.foreman-tool-tab:hover {
  color: var(--color-brass);
  background: rgba(232, 170, 0, 0.06);
}

.foreman-tool-tab.active {
  color: var(--color-brass-light);
  background: rgba(232, 170, 0, 0.1);
  border-color: var(--color-brass);
}

.foreman-hint {
  font-family: var(--font-mono, monospace);
  font-size: 9px;
  color: var(--color-text-dim);
  font-style: italic;
  margin: 0 0 3px;
  line-height: 1.4;
}

.discord-loading,
.discord-empty {
  font-family: var(--font-mono, monospace);
  font-size: 10px;
  color: var(--color-text-dim);
}

.discord-error {
  font-family: var(--font-mono, monospace);
  font-size: 10px;
  color: var(--color-red, #e74c3c);
}

.discord-status-grid {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  background: var(--color-bg);
  border: 1px solid var(--color-brass-dark);
  border-radius: 2px;
}

.discord-status-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
}

.discord-status-label {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-brass-dark);
  letter-spacing: 1px;
  text-transform: uppercase;
  flex-shrink: 0;
}

.discord-status-value {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-family: var(--font-mono, monospace);
  font-size: 11px;
  color: var(--color-text);
  text-align: right;
}

.discord-status-value--mono {
  word-break: break-all;
}

.status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex-shrink: 0;
  background: var(--color-text-dim);
}

.status-dot--ok {
  background: var(--color-green, #2ecc71);
}

.status-dot--off {
  background: var(--color-red, #e74c3c);
}

.discord-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.discord-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  background: var(--color-bg);
  border: 1px solid var(--color-brass-dark);
  border-radius: 2px;
}

.discord-avatar {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  border: 1px solid var(--color-teal);
  flex-shrink: 0;
}

.discord-avatar--placeholder {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: var(--color-bg-secondary);
  color: var(--color-text-dim);
  font-family: var(--font-mono, monospace);
  font-size: 8px;
}

.discord-row-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.discord-row-title {
  font-family: var(--font-mono, monospace);
  font-size: 11px;
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.discord-row-title--mono {
  font-size: 10px;
}

.discord-row-sub {
  font-family: var(--font-mono, monospace);
  font-size: 9px;
  color: var(--color-text-dim);
}

.discord-role-badge {
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

.discord-role-badge--owner {
  color: var(--color-brass-light);
  border-color: var(--color-brass);
}

.discord-remove-btn {
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

.discord-remove-btn:hover:not(:disabled) {
  border-color: var(--color-red, #c0392b);
  color: var(--color-red, #c0392b);
}

.discord-remove-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.discord-add-form {
  display: flex;
  flex-direction: column;
  gap: 4px;
  border-top: 1px solid var(--color-brass-dark);
  padding-top: 8px;
}

.discord-add-row {
  display: flex;
  align-items: center;
  gap: 4px;
}

.settings-input {
  flex: 1;
  background: var(--color-bg);
  border: 1px solid var(--color-brass-dark);
  color: var(--color-text);
  font-family: var(--font-mono, monospace);
  font-size: 11px;
  padding: 5px 7px;
  outline: none;
  border-radius: 2px;
  min-width: 0;
}

.settings-input:focus {
  border-color: var(--color-brass);
}

@media (prefers-reduced-motion: reduce) {
  .foreman-tool-tab,
  .discord-remove-btn {
    transition: none;
  }
}
</style>
