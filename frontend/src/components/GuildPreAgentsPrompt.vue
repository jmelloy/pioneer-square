<template>
  <div v-if="canManage" class="gpap">
    <div class="gpap-title">Pre-AGENTS prompt</div>
    <p class="gpap-intro">
      Guild-wide default text prepended to every task's prompt, before the repository's
      <code>AGENTS.md</code> is read. Leave empty for no injection.
    </p>

    <div v-if="loading" class="gpap-hint">Loading…</div>
    <template v-else>
      <textarea
        v-model="draft"
        class="gpap-textarea"
        rows="8"
        :maxlength="MAX_LEN"
        placeholder="e.g. Always run the linter before committing."
        aria-label="Pre-AGENTS prompt"
      />
      <div class="gpap-meta">
        <span>{{ draft.length }} / {{ MAX_LEN }}</span>
        <span v-if="updatedAt" class="gpap-updated">
          Last updated {{ new Date(updatedAt).toLocaleString() }}
        </span>
        <span v-else class="gpap-updated">Not set</span>
      </div>

      <div class="gpap-actions">
        <button class="pixel-btn gpap-save-btn" :disabled="saving || !dirty" @click="save(draft)">
          {{ saving ? 'Saving…' : 'Save' }}
        </button>
        <button
          class="pixel-btn gpap-clear-btn"
          :disabled="saving || (!saved && !draft)"
          @click="clear"
        >
          Clear
        </button>
        <span v-if="status" class="save-status" :class="'save-status-' + status">
          {{ status === 'saved' ? 'Saved' : 'Error' }}
        </span>
      </div>
      <div v-if="errorMsg" class="gpap-error">{{ errorMsg }}</div>

      <div class="gpap-precedence">
        <div class="gpap-sublabel">Precedence (first wins)</div>
        <ol>
          <li>Per-task override in the task payload</li>
          <li>Worker <code>pre_agents_prompt</code> in pioneer-worker.toml</li>
          <li>Worker env var <code>PIONEER_PRE_AGENTS_PROMPT</code></li>
          <li><strong>This guild default</strong></li>
          <li>No injection</li>
        </ol>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { api, ApiError } from '../utils/api'
import { useAuthStore } from '../stores/auth'

// Keep in sync with PRE_AGENTS_PROMPT_MAX_LEN in backend/routes/guilds.py.
const MAX_LEN = 20000

interface GuildSettings {
  pre_agents_prompt: string | null
  pre_agents_prompt_updated_at: string | null
}
interface GuildMemberRow {
  user_id: string
  role: string
}

const props = defineProps<{ guildId: string }>()
const authStore = useAuthStore()

const loading = ref(true)
const saving = ref(false)
const status = ref<'' | 'saved' | 'error'>('')
const errorMsg = ref('')
const myRole = ref<string | null>(null)
const saved = ref('')
const draft = ref('')
const updatedAt = ref<string | null>(null)

// Only owners (guild admins) may view or edit; the backend enforces this on PATCH.
const canManage = computed(() => myRole.value === 'owner')
const dirty = computed(() => draft.value !== saved.value)

const base = () => `/api/guilds/${encodeURIComponent(props.guildId)}`

function apply(s: GuildSettings) {
  saved.value = s.pre_agents_prompt ?? ''
  draft.value = saved.value
  updatedAt.value = s.pre_agents_prompt_updated_at ?? null
}

onMounted(async () => {
  try {
    const members = await api<GuildMemberRow[]>(`${base()}/members`)
    myRole.value = members.find((m) => m.user_id === authStore.user?.id)?.role ?? null
    if (myRole.value === 'owner') apply(await api<GuildSettings>(`${base()}/settings`))
  } catch {
    myRole.value = null
  } finally {
    loading.value = false
  }
})

async function save(value: string) {
  saving.value = true
  status.value = ''
  errorMsg.value = ''
  try {
    apply(
      await api<GuildSettings>(`${base()}/settings`, {
        method: 'PATCH',
        json: { pre_agents_prompt: value.trim() ? value : null },
      }),
    )
    status.value = 'saved'
  } catch (e) {
    status.value = 'error'
    errorMsg.value = e instanceof ApiError ? e.message : 'Failed to save'
  } finally {
    saving.value = false
  }
}

function clear() {
  draft.value = ''
  return save('')
}
</script>

<style scoped>
.gpap {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  background: var(--color-bg);
  border: 1px solid var(--color-brass-dark);
  border-radius: 2px;
}
.gpap-title,
.gpap-sublabel {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-teal);
  letter-spacing: 1px;
  text-transform: uppercase;
}
.gpap-intro,
.gpap-hint,
.gpap-precedence {
  font-size: 10px;
  color: var(--color-text-dim);
  line-height: 1.4;
  margin: 0;
}
.gpap-precedence ol {
  margin: 4px 0 0;
  padding-left: 18px;
}
.gpap-textarea {
  width: 100%;
  box-sizing: border-box;
  resize: vertical;
  background: var(--color-bg-secondary);
  color: inherit;
  border: 1px solid var(--color-brass-dark);
  border-radius: 2px;
  padding: 6px;
  font-family: var(--font-mono, monospace);
  font-size: 11px;
}
.gpap-meta {
  display: flex;
  justify-content: space-between;
  font-size: 9px;
  color: var(--color-text-dim);
}
.gpap-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.gpap-error {
  font-size: 10px;
  color: var(--color-red, #e55);
}
</style>
