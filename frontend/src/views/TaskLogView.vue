<template>
  <div class="task-log">
    <div v-if="loading" class="task-log-notice">Loading task log…</div>

    <div v-else-if="error" class="task-log-notice task-log-notice--error">
      <div>{{ error }}</div>
      <button v-if="needsLogin" class="pixel-btn" @click="signIn">Sign in with GitHub</button>
      <button v-else class="pixel-btn" @click="router.push('/')">Go to Pioneer Square</button>
    </div>

    <template v-else-if="task">
      <PaneHeader
        icon="▤"
        :title-text="task.name || task.id"
        :entity-id="task.id"
        :entity-state="task.state"
      />

      <div class="meta">
        <div v-for="f in metaFields" :key="f.label" class="meta-field">
          <span class="meta-label">{{ f.label }}</span>
          <a v-if="f.href" :href="f.href" target="_blank" rel="noopener noreferrer">{{
            f.value
          }}</a>
          <span v-else>{{ f.value }}</span>
        </div>
        <button class="pixel-btn copy-btn" @click="copyUrl">
          {{ copied ? 'COPIED' : 'COPY LINK' }}
        </button>
      </div>

      <LogList :logs="logs" />
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import PaneHeader from '../components/log-pane/PaneHeader.vue'
import LogList from '../components/log-pane/LogList.vue'
import { useAuthStore } from '../stores/auth'
import { useGitHubStore } from '../stores/github'
import { api, ApiError } from '../utils/api'
import type { LogEntry } from '../types'

interface TaskMeta {
  id: string
  name: string | null
  description: string
  guild_id: string
  worker_id: string | null
  state: string
  phase: string | null
  tool: string | null
  model: string | null
  branch: string | null
  created_at: string
  updated_at: string
  issue_number: number | null
  issue_repo: string | null
  issue_title: string | null
  pr_url: string | null
}

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const ghStore = useGitHubStore()

const task = ref<TaskMeta | null>(null)
const logs = ref<LogEntry[]>([])
const loading = ref(true)
const error = ref('')
const needsLogin = ref(false)
const copied = ref(false)

const taskId = computed(() => String(route.params.id))

function formatDateTime(iso?: string | null) {
  return iso ? new Date(iso).toLocaleString() : '—'
}

const metaFields = computed(() => {
  const t = task.value
  if (!t) return []
  const fields: { label: string; value: string; href?: string }[] = [
    { label: 'ID', value: t.id },
    { label: 'GUILD', value: t.guild_id },
    { label: 'WORKER', value: t.worker_id || '—' },
    { label: 'STATE', value: t.state },
    { label: 'PHASE', value: t.phase || '—' },
    { label: 'CREATED', value: formatDateTime(t.created_at) },
    { label: 'UPDATED', value: formatDateTime(t.updated_at) },
  ]
  if (t.branch) fields.push({ label: 'BRANCH', value: t.branch })
  if (t.issue_number && t.issue_repo) {
    fields.push({
      label: 'ISSUE',
      value: `${t.issue_repo}#${t.issue_number}`,
      href: `https://github.com/${t.issue_repo}/issues/${t.issue_number}`,
    })
  }
  if (t.pr_url)
    fields.push({ label: 'PR', value: t.pr_url.split('/').slice(-3).join('/'), href: t.pr_url })
  return fields
})

async function copyUrl() {
  await navigator.clipboard.writeText(window.location.href)
  copied.value = true
  setTimeout(() => (copied.value = false), 1500)
}

function signIn() {
  return authStore.loginWithGitHub(window.location.href)
}

async function load() {
  try {
    const data = await api<{ task: TaskMeta; logs: LogEntry[] }>(`/api/task/${taskId.value}/log`)
    task.value = data.task
    logs.value = data.logs
  } catch (e: unknown) {
    const status = e instanceof ApiError ? e.status : 0
    needsLogin.value = status === 401
    error.value =
      status === 401
        ? 'Sign in to view this task log.'
        : status === 404
          ? `Task ${taskId.value} not found.`
          : status === 403
            ? 'You are not a member of the guild that owns this task.'
            : e instanceof Error
              ? e.message
              : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  // GitHub OAuth bounces back here with the session in the query string
  // (see routes/auth.py github_callback), same handshake LandingView does.
  const params = new URLSearchParams(window.location.search)
  if (params.has('login_token')) {
    authStore.restoreFromCallback(params)
    ghStore.restoreGitHubToken(params)
    window.history.replaceState({}, '', window.location.pathname)
  }
  await load()
})
</script>

<style scoped>
.task-log {
  display: flex;
  flex-direction: column;
  width: 100vw;
  height: 100dvh;
  background: var(--color-bg);
  color: var(--color-text);
}

.task-log-notice {
  margin: auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
  font-size: 13px;
  color: var(--color-text-dim);
}
.task-log-notice--error {
  color: var(--color-red);
}
.task-log-notice .pixel-btn {
  font-size: 9px;
  padding: 10px 20px;
}

.meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px 18px;
  padding: 8px 16px;
  background: var(--color-bg-secondary);
  border-bottom: 1px solid var(--color-brass-dark);
  flex-shrink: 0;
  font-family: var(--font-mono);
  font-size: 11px;
}

.meta-field {
  display: flex;
  align-items: baseline;
  gap: 6px;
}

.meta-label {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-text-dim);
}

.meta a {
  color: var(--color-teal);
}

.copy-btn {
  margin-left: auto;
  font-size: 7px;
  padding: 5px 10px;
}
</style>
