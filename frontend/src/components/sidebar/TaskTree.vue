<template>
  <div class="tasks-tree">
    <div v-if="loading && !treeData" class="empty-state">Loading…</div>
    <div v-else-if="isEmpty" class="empty-state">No tasks yet</div>

    <template v-else>
      <!-- Conversation groups -->
      <div v-for="node in activeConversations" :key="nodeKey(node)" class="tree-group">
        <div class="group-header" @click="toggle(nodeKey(node))">
          <span class="collapse-icon">{{ isExpanded(nodeKey(node)) ? '▾' : '▸' }}</span>
          <span class="conversation-icon">💬</span>
          <span class="conversation-name">{{ node.name || 'Unnamed conversation' }}</span>
          <span class="state-badge" :class="'badge-' + node.status">{{ node.status }}</span>
          <span class="group-count">{{ countTasks(node.tasks) }}</span>
          <button
            class="open-btn"
            title="Open conversation"
            @click.stop="uiStore.selectConversation(node.id)"
          >
            ↗
          </button>
        </div>

        <div v-if="isExpanded(nodeKey(node))" class="group-tasks">
          <TaskTreeRow v-for="task in node.tasks" :key="task.id" :task="task" :depth="0" />
        </div>
      </div>

      <!-- Ungrouped tasks -->
      <div v-if="treeData!.ungrouped.length" class="tree-group">
        <div class="group-header" @click="toggle('__ungrouped__')">
          <span class="collapse-icon">{{ isExpanded('__ungrouped__') ? '▾' : '▸' }}</span>
          <span class="conversation-name ungrouped-label">Ungrouped</span>
          <span class="group-count">{{ countTasks(treeData!.ungrouped) }}</span>
        </div>
        <div v-if="isExpanded('__ungrouped__')" class="group-tasks">
          <TaskTreeRow v-for="task in treeData!.ungrouped" :key="task.id" :task="task" :depth="0" />
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useGuildStore } from '../../stores/guild'
import { useTasksStore } from '../../stores/tasks'
import { useUiStore } from '../../stores/ui'
import { api } from '../../utils/api'
import type { TaskTreeData, ConversationGroupNode, TaskTreeNode } from '../../types'
import TaskTreeRow from './TaskTreeRow.vue'

const guildStore = useGuildStore()
const tasksStore = useTasksStore()
const uiStore = useUiStore()

function nodeKey(node: ConversationGroupNode): string {
  return `conv:${node.id}`
}

const treeData = ref<TaskTreeData | null>(null)
const loading = ref(false)

// ---------------------------------------------------------------------------
// Expand / collapse — persisted to localStorage
// ---------------------------------------------------------------------------
const LS_KEY = 'pioneer_tree_expanded'

function _loadExpanded(): Set<string> {
  try {
    const raw = localStorage.getItem(LS_KEY)
    return new Set(raw ? JSON.parse(raw) : [])
  } catch {
    return new Set()
  }
}

function _saveExpanded(s: Set<string>) {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify([...s]))
  } catch {
    // ignore quota errors
  }
}

const expandedKeys = ref<Set<string>>(_loadExpanded())

function isExpanded(key: string): boolean {
  // Default: expanded for all nodes unless explicitly collapsed
  return !expandedKeys.value.has(`__collapsed__${key}`)
}

function toggle(key: string) {
  const collapseKey = `__collapsed__${key}`
  const next = new Set(expandedKeys.value)
  if (next.has(collapseKey)) {
    next.delete(collapseKey)
  } else {
    next.add(collapseKey)
  }
  expandedKeys.value = next
  _saveExpanded(next)
}

// ---------------------------------------------------------------------------
// Data fetching
// ---------------------------------------------------------------------------
async function fetchTree(guildId: string) {
  if (!guildId) return
  loading.value = true
  try {
    treeData.value = await api<TaskTreeData>(`/guilds/${guildId}/tasks/tree`)
  } catch (e) {
    console.error('Failed to fetch task tree', e)
  } finally {
    loading.value = false
  }
}

// Debounce helper (avoids hammering the endpoint on every WS event)
let _debounceTimer: ReturnType<typeof setTimeout> | null = null
function debouncedFetch(guildId: string, delay = 1500) {
  if (_debounceTimer) clearTimeout(_debounceTimer)
  _debounceTimer = setTimeout(() => {
    _debounceTimer = null
    fetchTree(guildId)
  }, delay)
}

// Refetch when the guild changes (immediate)
watch(
  () => guildStore.currentGuild?.id,
  (guildId) => {
    treeData.value = null
    if (guildId) fetchTree(guildId)
  },
  { immediate: true },
)

// Single watcher covers both task count changes and individual state changes
const taskWatchKey = computed(
  () => `${tasksStore.tasks.length}:${tasksStore.tasks.map((t) => t.state).join(',')}`,
)
watch(taskWatchKey, () => {
  const guildId = guildStore.currentGuild?.id
  if (guildId) debouncedFetch(guildId)
})

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
const TERMINAL_STATES = new Set(['done', 'failed', 'cancelled', 'error'])

function hasActiveTasks(tasks: TaskTreeNode[]): boolean {
  return tasks.some((t) => !TERMINAL_STATES.has(t.state) || hasActiveTasks(t.children))
}

function countTasks(tasks: TaskTreeNode[]): number {
  let n = tasks.length
  for (const t of tasks) n += countTasks(t.children)
  return n
}

// Conversations with live work, active ones first, then most recently updated.
const activeConversations = computed(() =>
  (treeData.value?.conversations.filter((c) => hasActiveTasks(c.tasks)) ?? []).sort(
    (a, b) =>
      Number(b.status === 'active') - Number(a.status === 'active') ||
      b.updated_at.localeCompare(a.updated_at),
  ),
)

// Also true when treeData is null (fetch failed / no guild yet) so the
// template never reaches the v-else branch without data.
const isEmpty = computed(
  () =>
    !loading.value &&
    (!treeData.value || (activeConversations.value.length === 0 && treeData.value.ungrouped.length === 0)),
)
</script>

<style scoped>
.tasks-tree {
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

.tree-group {
  border-bottom: 1px solid rgba(255, 255, 255, 0.04);
}

.group-header {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 6px 10px;
  cursor: pointer;
  position: sticky;
  top: 0;
  background: var(--color-bg-secondary);
  border-bottom: 1px solid var(--color-brass-dark);
  z-index: 1;
  transition: background 0.12s;
}

.group-header:hover {
  background: rgba(232, 170, 0, 0.06);
}

.collapse-icon {
  font-size: 9px;
  color: var(--color-text-dim);
  flex-shrink: 0;
  width: 10px;
  user-select: none;
}

.conversation-name {
  font-size: 11px;
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1;
  min-width: 0;
}

.conversation-icon {
  font-size: 10px;
  flex-shrink: 0;
}

.open-btn {
  background: none;
  border: none;
  color: var(--color-text-dim);
  cursor: pointer;
  font-size: 11px;
  padding: 0 2px;
  flex-shrink: 0;
}

.open-btn:hover {
  color: var(--color-brass);
}

.ungrouped-label {
  color: var(--color-text-dim);
  font-style: italic;
}

.state-badge {
  font-family: var(--font-pixel);
  font-size: 5px;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  padding: 1px 4px;
  border-radius: 2px;
  flex-shrink: 0;
}

.badge-active {
  background: rgba(80, 200, 120, 0.2);
  color: var(--color-green);
}

.badge-archived {
  background: rgba(150, 150, 150, 0.2);
  color: var(--color-text-dim);
}

.badge-closed {
  background: rgba(200, 80, 80, 0.2);
  color: var(--color-red);
}

.group-count {
  font-size: 9px;
  color: var(--color-text-dim);
  background: var(--color-bg);
  padding: 1px 5px;
  border-radius: 2px;
  flex-shrink: 0;
}

.group-tasks {
  /* tasks rendered by TaskTreeRow */
}
</style>
