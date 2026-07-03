<template>
  <div class="issue-viewer">
    <div v-if="loading && !issue" class="issue-loading">Loading issue...</div>
    <div v-else-if="loadError" class="issue-error">{{ loadError }}</div>

    <template v-else-if="issue">
      <!-- Header -->
      <div class="issue-header">
        <div class="issue-meta-row">
          <span class="issue-repo-label">{{ owner }}/{{ repo }}</span>
          <span class="issue-number">#{{ issueNumber }}</span>
          <span class="issue-state" :class="issue.state">{{ issue.state }}</span>
          <a :href="issue.html_url" target="_blank" rel="noopener noreferrer" class="gh-link">
            ↗ GitHub
          </a>
        </div>

        <!-- Title -->
        <div v-if="!editingTitle" class="issue-title-row">
          <h2 class="issue-title">{{ issue.title }}</h2>
          <button class="pixel-btn edit-btn" @click="startEditTitle" title="Edit title">✎</button>
        </div>
        <div v-else class="issue-title-edit">
          <input
            v-model="editTitle"
            class="title-input"
            @keydown.enter="saveTitle"
            @keydown.escape="cancelEditTitle"
            ref="titleInputRef"
          />
          <div class="edit-actions">
            <button class="pixel-btn save-btn" @click="saveTitle" :disabled="saving">
              {{ saving ? '…' : 'Save' }}
            </button>
            <button class="pixel-btn cancel-btn" @click="cancelEditTitle">Cancel</button>
          </div>
        </div>

        <div class="issue-submeta">
          <img :src="issue.user.avatar_url" class="avatar" :alt="issue.user.login" />
          <span class="issue-author">{{ issue.user.login }}</span>
          <span class="issue-date">opened {{ formatRelative(issue.created_at) }}</span>
          <span
            v-for="label in issue.labels"
            :key="label.id"
            class="issue-label"
            :style="{ borderColor: '#' + label.color, color: '#' + label.color }"
            >{{ label.name }}</span
          >
        </div>
      </div>

      <!-- Tab bar -->
      <div class="issue-tab-bar">
        <button
          class="issue-tab-btn"
          :class="{ active: activeTab === 'details' }"
          @click="activeTab = 'details'"
        >
          Details
        </button>
        <button
          class="issue-tab-btn"
          :class="{ active: activeTab === 'comments' }"
          @click="activeTab = 'comments'"
        >
          Comments ({{ comments.length }})
        </button>
        <button
          class="issue-tab-btn"
          :class="{ active: activeTab === 'tasklog' }"
          @click="onTaskLogTab"
        >
          Task Log{{ issueTasks.length > 0 ? ` (${issueTasks.length})` : '' }}
        </button>
      </div>

      <!-- Scrollable tab content -->
      <div class="issue-tab-content">
        <!-- Details tab -->
        <div v-if="activeTab === 'details'" class="issue-body-section">
          <div class="section-header">
            <span class="section-title">Description</span>
            <button
              v-if="!editingBody"
              class="pixel-btn edit-btn"
              @click="startEditBody"
              title="Edit body"
            >
              ✎
            </button>
          </div>
          <template v-if="!editingBody">
            <div
              v-if="issue.body"
              class="issue-body markdown-body"
              v-html="renderMarkdown(issue.body)"
            ></div>
            <div v-else class="issue-body-empty">No description provided.</div>
          </template>
          <div v-else class="body-edit">
            <textarea
              v-model="editBody"
              class="body-textarea"
              rows="10"
              ref="bodyTextareaRef"
            ></textarea>
            <div class="edit-actions">
              <button class="pixel-btn save-btn" @click="saveBody" :disabled="saving">
                {{ saving ? '…' : 'Save' }}
              </button>
              <button class="pixel-btn cancel-btn" @click="cancelEditBody">Cancel</button>
            </div>
          </div>
        </div>

        <!-- Task Log tab -->
        <template v-if="activeTab === 'tasklog'">
          <div class="section-header">
            <span class="section-title">Task Log</span>
            <button class="pixel-btn edit-btn" @click="loadIssueTasks" title="Refresh">↻</button>
          </div>
          <div v-if="tasklogLoading" class="tl-empty">Loading tasks…</div>
          <div v-else-if="issueTasks.length === 0" class="tl-empty">
            No tasks found for this issue.
          </div>
          <div v-else class="tl-list">
            <div v-for="task in issueTasks" :key="task.id" class="tl-row">
              <div class="tl-row-header" @click="toggleTask(task.id)">
                <span class="tl-expand">{{ expandedTaskIds.has(task.id) ? '▾' : '▸' }}</span>
                <span class="tl-name">{{ task.name || task.id }}</span>
                <span v-if="task.phase" class="tl-phase">{{ task.phase }}</span>
                <span class="tl-state" :class="'tl-state-' + task.state.replace(/[^a-z]/g, '-')">{{
                  tasksStore.stateLabel(task.state)
                }}</span>
                <span v-if="task.worker_id" class="tl-worker">{{ task.worker_id }}</span>
                <a
                  v-if="task.pr_url"
                  :href="task.pr_url"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="tl-pr"
                  @click.stop
                  >PR ↗</a
                >
                <span class="tl-date">{{ formatRelative(task.created_at ?? '') }}</span>
              </div>
              <div v-if="expandedTaskIds.has(task.id)" class="tl-logs">
                <div v-if="taskLogsFetching.has(task.id)" class="tl-logs-loading">
                  Fetching logs…
                </div>
                <pre v-else-if="taskLogsMap[task.id]?.length" class="tl-logs-pre"><code>{{
                  taskLogsMap[task.id].slice(-50).map((l) => l.line).join('\n')
                }}</code></pre>
                <div v-else class="tl-logs-empty">No log lines stored for this task.</div>
              </div>
            </div>
          </div>
        </template>

        <!-- Comments tab -->
        <template v-if="activeTab === 'comments'">
          <div class="comments-section">
            <div class="section-header">
              <span class="section-title">Comments ({{ comments.length }})</span>
            </div>
            <div class="comments-list">
              <div v-for="comment in comments" :key="comment.id" class="comment">
                <div class="comment-header">
                  <img :src="comment.user.avatar_url" class="avatar" :alt="comment.user.login" />
                  <span class="comment-author">{{ comment.user.login }}</span>
                  <span class="comment-date">{{ formatRelative(comment.created_at) }}</span>
                  <a
                    :href="comment.html_url"
                    target="_blank"
                    rel="noopener noreferrer"
                    class="gh-link-sm"
                    >↗</a
                  >
                </div>
                <div class="comment-body markdown-body" v-html="renderMarkdown(comment.body)"></div>
              </div>
              <div v-if="comments.length === 0" class="no-comments">No comments yet.</div>
            </div>
          </div>

          <!-- Post comment -->
          <div class="post-comment-section">
            <div class="section-header">
              <span class="section-title">Add a comment</span>
            </div>
            <textarea
              v-model="newComment"
              class="comment-textarea"
              rows="4"
              placeholder="Leave a comment..."
            ></textarea>
            <div class="post-actions">
              <button
                class="pixel-btn submit-btn"
                @click="submitComment"
                :disabled="!newComment.trim() || submitting"
              >
                {{ submitting ? 'Posting…' : 'Comment' }}
              </button>
              <span v-if="postError" class="post-error">{{ postError }}</span>
            </div>
          </div>
        </template>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, nextTick } from 'vue'
import { useGitHubStore } from '../stores/github'
import type { GitHubIssueDetail, GitHubComment } from '../stores/github'
import { useGuildStore } from '../stores/guild'
import { useTasksStore } from '../stores/tasks'
import type { Task, LogEntry } from '../types'
import { api } from '../utils/api'
import { renderMarkdown } from '../utils/markdown'
import { formatRelative } from '../utils/format'

const props = defineProps<{
  owner: string
  repo: string
  issueNumber: number
}>()

const ghStore = useGitHubStore()
const guildStore = useGuildStore()
const tasksStore = useTasksStore()

const issue = ref<GitHubIssueDetail | null>(null)
const comments = ref<GitHubComment[]>([])
const loading = ref(false)
const loadError = ref('')

const activeTab = ref<'details' | 'comments' | 'tasklog'>('details')

// Task Log tab state
const issueTasks = ref<Task[]>([])
const tasklogLoading = ref(false)
const expandedTaskIds = ref<Set<string>>(new Set())
const taskLogsMap = ref<Record<string, LogEntry[]>>({})
const taskLogsFetching = ref<Set<string>>(new Set())

async function loadIssueTasks() {
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  tasklogLoading.value = true
  try {
    const issueRepo = `${props.owner}/${props.repo}`
    const raw = await api<Task[]>(
      `/guilds/${guildId}/tasks?issue_number=${props.issueNumber}&issue_repo=${encodeURIComponent(issueRepo)}`,
    )
    // Sort oldest first for chronological display
    issueTasks.value = [...raw].sort((a, b) => {
      const ta = a.created_at ? new Date(a.created_at).getTime() : 0
      const tb = b.created_at ? new Date(b.created_at).getTime() : 0
      return ta - tb
    })
  } catch (e) {
    console.error('Failed to fetch issue tasks', e)
  } finally {
    tasklogLoading.value = false
  }
}

function onTaskLogTab() {
  activeTab.value = 'tasklog'
  if (issueTasks.value.length === 0 && !tasklogLoading.value) {
    loadIssueTasks()
  }
}

async function toggleTask(taskId: string) {
  if (expandedTaskIds.value.has(taskId)) {
    expandedTaskIds.value = new Set([...expandedTaskIds.value].filter((x) => x !== taskId))
    return
  }
  expandedTaskIds.value = new Set(expandedTaskIds.value).add(taskId)
  if (taskLogsMap.value[taskId] !== undefined) return
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  taskLogsFetching.value = new Set(taskLogsFetching.value).add(taskId)
  try {
    const raw = await api<
      Array<{ line: string; timestamp: string; detail?: unknown; level?: unknown }>
    >(`/guilds/${guildId}/tasks/${taskId}/logs`)
    taskLogsMap.value[taskId] = raw.map((r) => ({
      line: r.line,
      timestamp: r.timestamp,
      detail: (r.detail as LogEntry['detail']) || null,
      level: (r.level as LogEntry['level']) || null,
    }))
  } catch (e) {
    console.error('Failed to fetch task logs', e)
    taskLogsMap.value[taskId] = []
  } finally {
    taskLogsFetching.value = new Set([...taskLogsFetching.value].filter((x) => x !== taskId))
  }
}

const editingTitle = ref(false)
const editTitle = ref('')
const editingBody = ref(false)
const editBody = ref('')
const saving = ref(false)

const newComment = ref('')
const submitting = ref(false)
const postError = ref('')

const titleInputRef = ref<HTMLInputElement | null>(null)
const bodyTextareaRef = ref<HTMLTextAreaElement | null>(null)

async function loadIssue() {
  loading.value = true
  loadError.value = ''
  try {
    const data = await ghStore.fetchIssueDetail(props.owner, props.repo, props.issueNumber)
    issue.value = data.issue
    comments.value = data.comments
  } catch (e: unknown) {
    loadError.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

function startEditTitle() {
  editTitle.value = issue.value?.title ?? ''
  editingTitle.value = true
  nextTick(() => titleInputRef.value?.focus())
}

function cancelEditTitle() {
  editingTitle.value = false
}

async function saveTitle() {
  if (!issue.value || !editTitle.value.trim()) return
  saving.value = true
  try {
    const updated = await ghStore.updateIssue(props.owner, props.repo, props.issueNumber, {
      title: editTitle.value.trim(),
    })
    issue.value = updated
    editingTitle.value = false
  } catch (e: unknown) {
    loadError.value = e instanceof Error ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

function startEditBody() {
  editBody.value = issue.value?.body ?? ''
  editingBody.value = true
  nextTick(() => bodyTextareaRef.value?.focus())
}

function cancelEditBody() {
  editingBody.value = false
}

async function saveBody() {
  if (!issue.value) return
  saving.value = true
  try {
    const updated = await ghStore.updateIssue(props.owner, props.repo, props.issueNumber, {
      body: editBody.value,
    })
    issue.value = updated
    editingBody.value = false
  } catch (e: unknown) {
    loadError.value = e instanceof Error ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

async function submitComment() {
  const body = newComment.value.trim()
  if (!body) return
  submitting.value = true
  postError.value = ''
  try {
    const comment = await ghStore.postComment(props.owner, props.repo, props.issueNumber, body)
    comments.value.push(comment)
    newComment.value = ''
  } catch (e: unknown) {
    postError.value = e instanceof Error ? e.message : String(e)
  } finally {
    submitting.value = false
  }
}

onMounted(loadIssue)
</script>

<style scoped>
.issue-viewer {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  font-size: 13px;
  color: var(--color-text);
}

.issue-loading,
.issue-error {
  color: var(--color-text-dim);
  text-align: center;
  margin-top: 40px;
  font-style: italic;
}

.issue-error {
  color: var(--color-red);
}

/* Header */
.issue-header {
  display: flex;
  flex-direction: column;
  gap: 8px;
  border-bottom: 1px solid var(--color-brass-dark);
  padding: 16px 20px 14px;
  flex-shrink: 0;
}

.issue-meta-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.issue-repo-label {
  font-size: 10px;
  color: var(--color-text-dim);
}

.issue-number {
  font-family: var(--font-pixel);
  font-size: 8px;
  color: var(--color-brass-dark);
}

.issue-state {
  font-size: 9px;
  padding: 2px 6px;
  border-radius: 10px;
  border: 1px solid;
}

.issue-state.open {
  color: var(--color-green);
  border-color: var(--color-green);
}

.issue-state.closed {
  color: var(--color-red);
  border-color: var(--color-red);
}

.gh-link {
  font-size: 10px;
  color: var(--color-teal);
  text-decoration: none;
  margin-left: auto;
}
.gh-link:hover {
  text-decoration: underline;
}

.gh-link-sm {
  font-size: 10px;
  color: var(--color-teal);
  text-decoration: none;
  margin-left: auto;
  opacity: 0.7;
}
.gh-link-sm:hover {
  opacity: 1;
}

.issue-title-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

.issue-title {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  line-height: 1.35;
  flex: 1;
}

.issue-title-edit {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.title-input {
  font-size: 15px;
  font-family: var(--font-mono);
  padding: 6px 8px;
  background: var(--color-bg-secondary);
  border: 1px solid var(--color-brass-dark);
  color: var(--color-text);
  border-radius: 3px;
  width: 100%;
}

.title-input:focus {
  outline: none;
  border-color: var(--color-brass);
}

.issue-submeta {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.avatar {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  object-fit: cover;
}

.issue-author {
  font-size: 11px;
  font-weight: 600;
  color: var(--color-text);
}

.issue-date {
  font-size: 10px;
  color: var(--color-text-dim);
}

.issue-label {
  font-size: 9px;
  border: 1px solid;
  padding: 1px 6px;
  border-radius: 3px;
}

/* Tab bar */
.issue-tab-bar {
  display: flex;
  background: var(--color-bg-secondary);
  border-bottom: 1px solid var(--color-brass-dark);
  flex-shrink: 0;
}

.issue-tab-btn {
  padding: 8px 16px;
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  color: var(--color-text-dim);
  cursor: pointer;
  font-family: var(--font-mono);
  font-size: 11px;
  transition: all 0.15s;
  margin-bottom: -1px;
}

.issue-tab-btn:hover {
  color: var(--color-text);
  background: rgba(232, 170, 0, 0.05);
}

.issue-tab-btn.active {
  color: var(--color-brass-light);
  border-bottom-color: var(--color-brass);
}

/* Scrollable tab content */
.issue-tab-content {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px;
  display: flex;
  flex-direction: column;
  gap: 20px;
  min-height: 0;
}

/* Section layout */
.issue-body-section,
.comments-section,
.post-comment-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.section-header {
  display: flex;
  align-items: center;
  gap: 8px;
}

.section-title {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-brass-light);
  letter-spacing: 1.5px;
  text-transform: uppercase;
}

.edit-btn {
  font-size: 10px;
  padding: 2px 6px;
  margin-left: auto;
}

/* Body */
.issue-body {
  padding: 12px;
  background: var(--color-bg-secondary);
  border: 1px solid var(--color-bg-tertiary);
  border-radius: 3px;
}

.issue-body-empty {
  color: var(--color-text-dim);
  font-style: italic;
  font-size: 12px;
  padding: 8px;
}

.body-edit {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.body-textarea,
.comment-textarea {
  font-family: var(--font-mono);
  font-size: 12px;
  padding: 8px;
  background: var(--color-bg-secondary);
  border: 1px solid var(--color-brass-dark);
  color: var(--color-text);
  border-radius: 3px;
  resize: vertical;
  width: 100%;
  box-sizing: border-box;
}

.body-textarea:focus,
.comment-textarea:focus {
  outline: none;
  border-color: var(--color-brass);
}

.edit-actions,
.post-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.save-btn {
  background: rgba(0, 187, 170, 0.15);
  border-color: var(--color-teal);
  color: var(--color-teal);
}

.save-btn:hover:not(:disabled) {
  background: rgba(0, 187, 170, 0.25);
}

.cancel-btn {
  opacity: 0.6;
}

.cancel-btn:hover {
  opacity: 1;
}

.submit-btn {
  background: rgba(0, 187, 170, 0.15);
  border-color: var(--color-teal);
  color: var(--color-teal);
}

.submit-btn:hover:not(:disabled) {
  background: rgba(0, 187, 170, 0.25);
}

.submit-btn:disabled,
.save-btn:disabled {
  opacity: 0.4;
  pointer-events: none;
}

.post-error {
  font-size: 11px;
  color: var(--color-red);
}

/* Comments */
.comments-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.comment {
  border: 1px solid var(--color-bg-tertiary);
  border-radius: 3px;
  overflow: hidden;
}

.comment-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  background: var(--color-bg-secondary);
  border-bottom: 1px solid var(--color-bg-tertiary);
}

.comment-author {
  font-size: 11px;
  font-weight: 600;
}

.comment-date {
  font-size: 10px;
  color: var(--color-text-dim);
}

.comment-body {
  padding: 10px 12px;
}

.no-comments {
  font-size: 11px;
  color: var(--color-text-dim);
  font-style: italic;
  text-align: center;
  padding: 12px;
}

/* Markdown styles shared by body and comments */
.markdown-body :deep(p) {
  margin: 0 0 8px;
  line-height: 1.5;
}

.markdown-body :deep(p:last-child) {
  margin-bottom: 0;
}

.markdown-body :deep(ul),
.markdown-body :deep(ol) {
  margin: 0 0 8px;
  padding-left: 20px;
}

.markdown-body :deep(li) {
  margin-bottom: 3px;
  line-height: 1.5;
}

.markdown-body :deep(code) {
  font-family: var(--font-mono);
  font-size: 11px;
  background: var(--color-bg-tertiary);
  padding: 1px 4px;
  border-radius: 2px;
}

.markdown-body :deep(pre) {
  background: var(--color-bg-tertiary);
  border: 1px solid var(--color-bg);
  border-radius: 3px;
  padding: 10px;
  overflow-x: auto;
  margin: 0 0 8px;
}

.markdown-body :deep(pre code) {
  background: none;
  padding: 0;
}

.markdown-body :deep(blockquote) {
  border-left: 3px solid var(--color-brass-dark);
  margin: 0 0 8px;
  padding: 4px 10px;
  color: var(--color-text-dim);
}

.markdown-body :deep(h1),
.markdown-body :deep(h2),
.markdown-body :deep(h3) {
  margin: 10px 0 6px;
  font-size: 13px;
  color: var(--color-brass-light);
}

.markdown-body :deep(a) {
  color: var(--color-teal);
  text-decoration: none;
}

.markdown-body :deep(a:hover) {
  text-decoration: underline;
}

.markdown-body :deep(hr) {
  border: none;
  border-top: 1px solid var(--color-bg-tertiary);
  margin: 10px 0;
}

.markdown-body :deep(table) {
  border-collapse: collapse;
  margin: 0 0 8px;
  font-size: 12px;
}

.markdown-body :deep(th),
.markdown-body :deep(td) {
  border: 1px solid var(--color-bg-tertiary);
  padding: 4px 8px;
}

.markdown-body :deep(th) {
  background: var(--color-bg-secondary);
}

/* Task Log tab */
.tl-empty {
  color: var(--color-text-dim);
  font-style: italic;
  font-size: 12px;
  text-align: center;
  padding: 20px 0;
}

.tl-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.tl-row {
  border: 1px solid var(--color-bg-tertiary);
  border-radius: 3px;
  overflow: hidden;
}

.tl-row-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 7px 10px;
  background: var(--color-bg-secondary);
  cursor: pointer;
  flex-wrap: wrap;
}

.tl-row-header:hover {
  background: rgba(232, 170, 0, 0.05);
}

.tl-expand {
  font-size: 10px;
  color: var(--color-text-dim);
  flex-shrink: 0;
  width: 10px;
}

.tl-name {
  font-size: 11px;
  font-weight: 600;
  color: var(--color-text);
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tl-phase {
  font-size: 9px;
  color: var(--color-text-dim);
  padding: 1px 5px;
  background: var(--color-bg-tertiary);
  border-radius: 2px;
  flex-shrink: 0;
}

.tl-state {
  font-size: 9px;
  padding: 1px 5px;
  border-radius: 2px;
  border: 1px solid;
  flex-shrink: 0;
}

.tl-state-pending {
  color: var(--color-text-dim);
  border-color: var(--color-text-dim);
}
.tl-state-planning {
  color: #4da6ff;
  border-color: #4da6ff;
}
.tl-state-working {
  color: var(--color-green);
  border-color: var(--color-green);
}
.tl-state-awaiting-review {
  color: #e8aa00;
  border-color: #e8aa00;
}
.tl-state-done {
  color: var(--color-teal);
  border-color: var(--color-teal);
}
.tl-state-failed,
.tl-state-cancelled {
  color: var(--color-red);
  border-color: var(--color-red);
}
.tl-state-followup {
  color: #ff9944;
  border-color: #ff9944;
}

.tl-worker {
  font-size: 9px;
  color: var(--color-text-dim);
  font-family: var(--font-mono);
  flex-shrink: 0;
}

.tl-pr {
  font-size: 9px;
  color: var(--color-teal);
  text-decoration: none;
  flex-shrink: 0;
}
.tl-pr:hover {
  text-decoration: underline;
}

.tl-date {
  font-size: 9px;
  color: var(--color-text-dim);
  margin-left: auto;
  flex-shrink: 0;
}

.tl-logs {
  border-top: 1px solid var(--color-bg-tertiary);
}

.tl-logs-loading,
.tl-logs-empty {
  font-size: 11px;
  color: var(--color-text-dim);
  font-style: italic;
  padding: 8px 12px;
}

.tl-logs-pre {
  margin: 0;
  padding: 10px 12px;
  background: var(--color-bg);
  font-family: var(--font-mono);
  font-size: 10px;
  line-height: 1.5;
  overflow-x: auto;
  max-height: 260px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-all;
}

.tl-logs-pre code {
  background: none;
  padding: 0;
}
</style>
