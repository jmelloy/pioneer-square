<template>
  <aside class="sidebar panel-bg">
    <div v-if="currentGuild" class="guild-name-bar">
      <template v-if="renamingGuild">
        <input
          ref="renameInput"
          v-model="renameValue"
          class="guild-rename-input"
          @keydown.enter="commitRename"
          @keydown.escape="cancelRename"
          @blur="commitRename"
        />
      </template>
      <template v-else>
        <span class="guild-name-text" @click="startRename" title="Click to rename">{{ currentGuild.name }}</span>
        <button class="rename-btn" @click="startRename" title="Rename guild">✎</button>
      </template>
    </div>

    <div class="sidebar-header">
      <span class="sidebar-title">Tasks</span>
      <button class="pixel-btn new-btn" @click="goHome">⌂ Home</button>
    </div>

    <div class="tasks-list">
      <div v-if="tasksStore.tasks.length === 0" class="empty-state">No tasks yet</div>

      <template v-for="group in groupedTasks" :key="group.label">
        <div class="date-separator">
          <span class="date-label">{{ group.label }}</span>
          <span class="date-count">{{ group.tasks.length }}</span>
        </div>
        <div
          v-for="task in group.tasks"
          :key="task.id"
          class="task-item"
          :class="{ selected: tasksStore.selectedTaskId === task.id }"
          @click="openTask(task.id)"
        >
          <div class="task-top">
            <span class="task-dot" :class="'dot-' + (task.state || 'pending').replace(/[^a-z]/g, '-')"></span>
            <span class="task-name">{{ task.name || task.id }}</span>
          </div>
          <div class="task-meta">
            <span class="task-state">{{ tasksStore.stateLabel(task.state) }}</span>
            <span class="task-time">{{ formatTime(task.created_at) }}</span>
          </div>
        </div>
      </template>
    </div>

    <!-- Workers/Agents hierarchical section -->
    <div v-if="agentsStore.workers.length > 0" class="workers-section">
      <div class="section-header">
        <span class="section-label">Workers</span>
        <span class="section-count">{{ agentsStore.workers.length }}</span>
      </div>
      <template v-for="worker in agentsStore.workers" :key="worker.id">
        <!-- Worker row -->
        <div class="worker-row" :class="worker.state" @click="agentsStore.selectWorker(worker.id)">
          <span class="worker-dot" :class="'wdot-' + worker.state"></span>
          <span class="worker-row-name">{{ worker.id }}</span>
          <span class="worker-row-state">{{ worker.state }}</span>
        </div>
        <!-- Agent rows under this worker -->
        <div
          v-for="agent in agentsForWorker(worker.id)"
          :key="agent.id"
          class="agent-row"
        >
          <span class="agent-dot" :class="'wdot-' + agent.state"></span>
          <span class="agent-row-name">{{ agent.name }}</span>
          <div class="agent-actions">
            <button
              class="agent-icon-btn"
              title="Open agent terminal"
              @click.stop="agentsStore.selectAgent(agent.id)"
            >🤖</button>
            <button
              class="agent-icon-btn"
              :disabled="!currentTaskForWorker(worker.id)"
              :title="currentTaskForWorker(worker.id) ? 'Open current task' : 'No active task'"
              @click.stop="openAgentTask(worker.id)"
            >📋</button>
          </div>
        </div>
      </template>
    </div>

    <div class="sidebar-footer">
      <div class="gh-block" @click="showGitHubModal = true" :title="authStore.user ? 'GitHub: ' + authStore.user.login : 'Configure GitHub'">
        <div class="gh-inner" :class="{ configured: authStore.isLoggedIn }">
          <img v-if="authStore.isLoggedIn" :src="authStore.user?.avatar_url" class="gh-avatar" alt="gh" />
          <span v-else class="gh-icon">⚙</span>
          <div class="gh-text">
            <span v-if="authStore.isLoggedIn" class="gh-login">{{ authStore.user?.login }}</span>
            <span v-else class="gh-setup">Connect GitHub</span>
            <span class="gh-repos" v-if="authStore.isLoggedIn && ghStore.selectedRepos.length > 0">
              {{ ghStore.selectedRepos.length }} repo{{ ghStore.selectedRepos.length !== 1 ? 's' : '' }}
            </span>
          </div>
        </div>
      </div>

      <div class="connection-status" :class="{ connected: isConnected }">
        <span class="status-dot"></span>
        {{ isConnected ? 'Connected' : 'Disconnected' }}
      </div>
    </div>
  </aside>

  <GitHubConfigModal v-if="showGitHubModal" @close="showGitHubModal = false" />
</template>

<script setup>
import { ref, computed, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { useGuildStore } from '../stores/guild.js'
import { useGitHubStore } from '../stores/github.js'
import { useAuthStore } from '../stores/auth.js'
import { useTasksStore } from '../stores/tasks.js'
import { useAgentsStore } from '../stores/agents.js'
import GitHubConfigModal from './GitHubConfigModal.vue'

const router = useRouter()
const guildStore = useGuildStore()
const ghStore = useGitHubStore()
const authStore = useAuthStore()
const tasksStore = useTasksStore()
const agentsStore = useAgentsStore()

const showGitHubModal = ref(false)
const currentGuild = computed(() => guildStore.currentGuild)
const isConnected = computed(() => guildStore.isConnected)

const renamingGuild = ref(false)
const renameValue = ref('')
const renameInput = ref(null)

async function startRename() {
  if (!currentGuild.value) return
  renameValue.value = currentGuild.value.name || ''
  renamingGuild.value = true
  await nextTick()
  renameInput.value?.select()
}

async function commitRename() {
  if (!renamingGuild.value) return
  renamingGuild.value = false
  const trimmed = renameValue.value.trim()
  if (trimmed && trimmed !== currentGuild.value?.name) {
    try {
      await guildStore.renameGuild(currentGuild.value.id, trimmed)
    } catch (e) {
      console.error('Failed to rename guild', e)
    }
  }
}

function cancelRename() {
  renamingGuild.value = false
}

const groupedTasks = computed(() => {
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const yesterday = new Date(today)
  yesterday.setDate(yesterday.getDate() - 1)
  const weekAgo = new Date(today)
  weekAgo.setDate(weekAgo.getDate() - 7)

  const groupMap = new Map()

  for (const task of tasksStore.tasks) {
    const d = task.created_at ? new Date(task.created_at) : new Date()
    const taskDay = new Date(d.getFullYear(), d.getMonth(), d.getDate())

    let label
    if (taskDay >= today) label = 'Today'
    else if (taskDay >= yesterday) label = 'Yesterday'
    else if (taskDay >= weekAgo) label = d.toLocaleDateString('en-US', { weekday: 'long' })
    else label = d.toLocaleDateString('en-US', { month: 'long', year: 'numeric' })

    if (!groupMap.has(label)) groupMap.set(label, [])
    groupMap.get(label).push(task)
  }

  return Array.from(groupMap.entries()).map(([label, tasks]) => ({ label, tasks }))
})

function goHome() {
  router.push('/')
}

function openTask(taskId) {
  tasksStore.selectTask(taskId)
}

function agentsForWorker(workerId) {
  return agentsStore.agents.filter(a => a.workerId === workerId)
}

function currentTaskForWorker(workerId) {
  const active = tasksStore.tasks.filter(
    t => t.worker_id === workerId && !['done', 'failed'].includes(t.state)
  )
  if (active.length) return active[0]
  return tasksStore.tasks.find(t => t.worker_id === workerId) || null
}

function openAgentTask(workerId) {
  const task = currentTaskForWorker(workerId)
  if (task) tasksStore.selectTask(task.id)
}

function formatTime(isoStr) {
  if (!isoStr) return ''
  const d = new Date(isoStr)
  const now = new Date()
  const diffMins = Math.floor((now - d) / 60000)
  if (diffMins < 1) return 'just now'
  if (diffMins < 60) return `${diffMins}m ago`
  const diffHours = Math.floor(diffMins / 60)
  if (diffHours < 24) return `${diffHours}h ago`
  return d.toLocaleDateString()
}
</script>

<style scoped>
.sidebar {
  width: 220px;
  min-width: 220px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.guild-name-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 10px 6px;
  border-bottom: 1px solid var(--color-brass-dark);
  background: var(--color-bg);
  flex-shrink: 0;
  min-height: 32px;
}

.guild-name-text {
  font-family: var(--font-pixel);
  font-size: 8px;
  color: var(--color-brass);
  letter-spacing: 1px;
  text-transform: uppercase;
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  cursor: pointer;
  text-shadow: 0 0 6px rgba(255, 214, 68, 0.3);
  transition: color 0.12s;
}

.guild-name-text:hover {
  color: var(--color-brass-light);
}

.rename-btn {
  background: none;
  border: none;
  color: var(--color-brass-dark);
  cursor: pointer;
  font-size: 12px;
  padding: 1px 3px;
  border-radius: 2px;
  line-height: 1;
  opacity: 0.5;
  transition: opacity 0.12s, background 0.12s;
  flex-shrink: 0;
}

.rename-btn:hover {
  opacity: 1;
  background: rgba(232, 170, 0, 0.1);
}

.guild-rename-input {
  flex: 1;
  background: var(--color-bg-secondary);
  border: 1px solid var(--color-brass);
  color: var(--color-brass-light);
  font-family: var(--font-pixel);
  font-size: 8px;
  letter-spacing: 1px;
  text-transform: uppercase;
  padding: 3px 6px;
  outline: none;
  border-radius: 2px;
  min-width: 0;
}

.sidebar-header {
  padding: 12px 10px;
  border-bottom: 2px solid var(--color-brass-dark);
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--color-bg-tertiary);
  flex-shrink: 0;
}

.sidebar-title {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-brass-light);
  letter-spacing: 2px;
  text-transform: uppercase;
  text-shadow: 0 0 6px rgba(255, 214, 68, 0.4);
}

.new-btn {
  font-size: 7px;
  padding: 4px 7px;
}

/* ── Unified task list ── */
.tasks-list {
  flex: 1;
  overflow-y: auto;
  padding: 0;
}

.empty-state {
  padding: 24px 12px;
  text-align: center;
  color: var(--color-text-dim);
  font-size: 11px;
  font-style: italic;
}

.date-separator {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 12px 4px;
  position: sticky;
  top: 0;
  background: var(--color-bg-secondary);
  border-bottom: 1px solid var(--color-brass-dark);
  z-index: 1;
}

.date-label {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-brass-dark);
  letter-spacing: 1.5px;
  text-transform: uppercase;
}

.date-count {
  font-size: 9px;
  color: var(--color-text-dim);
  background: var(--color-bg);
  padding: 1px 5px;
  border-radius: 2px;
}

.task-item {
  padding: 8px 12px;
  cursor: pointer;
  border-bottom: 1px solid rgba(255, 255, 255, 0.03);
  transition: background 0.12s;
}

.task-item:hover {
  background: rgba(232, 170, 0, 0.06);
}

.task-item.selected {
  background: rgba(232, 170, 0, 0.12);
  border-left: 3px solid var(--color-brass);
  padding-left: 9px;
}

.task-top {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 3px;
}

.task-dot {
  width: 6px;
  height: 6px;
  border-radius: 2px;
  flex-shrink: 0;
}
.dot-pending { background: var(--color-text-dim); }
.dot-planning { background: var(--color-blue); }
.dot-working { background: var(--color-green); animation: pulse 0.5s infinite; }
.dot-awaiting-review { background: var(--color-amber); animation: pulse 1.5s infinite; }
.dot-done { background: var(--color-teal); }
.dot-failed { background: var(--color-red); }
.dot-follow-up { background: var(--color-orange); animation: pulse 0.8s infinite; }
.dot-followup { background: var(--color-orange); animation: pulse 0.8s infinite; }

.task-name {
  font-size: 11px;
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1;
}

.task-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-left: 12px;
}

.task-state {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-brass-dark);
  letter-spacing: 0.5px;
  text-transform: uppercase;
}

.task-time {
  font-size: 9px;
  color: var(--color-text-dim);
  flex-shrink: 0;
}

/* ── Workers section ── */
.workers-section {
  border-top: 2px solid var(--color-brass-dark);
  flex-shrink: 0;
  max-height: 240px;
  overflow-y: auto;
}

.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 5px 12px 4px;
  background: var(--color-bg-secondary);
  border-bottom: 1px solid var(--color-brass-dark);
  position: sticky;
  top: 0;
}

.section-label {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-teal);
  letter-spacing: 1.5px;
  text-transform: uppercase;
}

.section-count {
  font-size: 9px;
  color: var(--color-text-dim);
  background: var(--color-bg);
  padding: 1px 5px;
  border-radius: 2px;
}

/* Worker top-level row */
.worker-row {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 5px 12px;
  background: var(--color-bg-secondary);
  border-bottom: 1px solid rgba(255, 255, 255, 0.04);
  cursor: pointer;
  transition: background 0.12s;
}

.worker-row:hover {
  background: rgba(0, 187, 170, 0.08);
}

.worker-row-name {
  font-size: 10px;
  color: var(--color-teal);
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-family: var(--font-pixel);
  letter-spacing: 0.5px;
}

.worker-row-state {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-text-dim);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

/* Agent row (indented under worker) */
.agent-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 12px 5px 22px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.02);
  transition: background 0.12s;
}

.agent-row:hover {
  background: rgba(0, 187, 170, 0.06);
}

.agent-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  flex-shrink: 0;
}

.agent-row-name {
  font-size: 10px;
  color: var(--color-text);
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.agent-actions {
  display: flex;
  gap: 2px;
  flex-shrink: 0;
}

.agent-icon-btn {
  background: none;
  border: none;
  cursor: pointer;
  font-size: 12px;
  padding: 1px 3px;
  border-radius: 2px;
  opacity: 0.6;
  transition: opacity 0.12s, background 0.12s;
  line-height: 1;
}

.agent-icon-btn:hover:not(:disabled) {
  opacity: 1;
  background: rgba(255, 255, 255, 0.08);
}

.agent-icon-btn:disabled {
  opacity: 0.2;
  cursor: not-allowed;
}

.worker-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}
.wdot-idle    { background: var(--color-text-dim); }
.wdot-working { background: var(--color-green); animation: pulse 0.5s infinite; }
.wdot-thinking { background: var(--color-blue); animation: pulse 1s infinite; }
.wdot-busy    { background: var(--color-orange); animation: pulse 0.8s infinite; }
.wdot-error   { background: var(--color-red); }
.wdot-offline { background: #333; }

/* ── Footer ── */
.sidebar-footer {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px 12px;
  border-top: 2px solid var(--color-brass-dark);
  background: var(--color-bg-tertiary);
  flex-shrink: 0;
}

/* ── GitHub block ── */
.gh-block {
  cursor: pointer;
  border: 1px solid var(--color-brass-dark);
  transition: all 0.15s;
  border-radius: 2px;
}

.gh-block:hover {
  border-color: var(--color-brass);
  background: rgba(232, 170, 0, 0.06);
}

.gh-inner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 9px;
}

.gh-inner.configured {
  border-left: 3px solid var(--color-teal);
}

.gh-avatar {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  border: 1px solid var(--color-teal);
  flex-shrink: 0;
}

.gh-icon {
  font-size: 16px;
  color: var(--color-brass-dark);
  flex-shrink: 0;
}

.gh-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  overflow: hidden;
}

.gh-login {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-teal);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.gh-setup {
  font-size: 10px;
  color: var(--color-text-dim);
}

.gh-repos {
  font-size: 9px;
  color: var(--color-text-dim);
}

/* ── Connection status ── */
.connection-status {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 10px;
  color: var(--color-red);
}

.connection-status.connected {
  color: var(--color-green);
}

.status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
  display: inline-block;
}

.connection-status.connected .status-dot {
  animation: pulse 2s infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.3; }
}
</style>
