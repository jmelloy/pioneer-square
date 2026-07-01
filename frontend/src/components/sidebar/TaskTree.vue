<template>
  <div class="tasks-tree">
    <div v-if="loading && !treeData" class="empty-state">Loading…</div>
    <div v-else-if="isEmpty" class="empty-state">No tasks yet</div>

    <template v-else>
      <!-- Issue / PR nodes -->
      <div
        v-for="node in treeData!.nodes"
        :key="`${node.issue_repo}#${node.issue_number}`"
        class="tree-group"
      >
        <div
          class="group-header"
          @click="toggle(`${node.issue_repo}#${node.issue_number}`)"
        >
          <span class="collapse-icon">{{
            isExpanded(`${node.issue_repo}#${node.issue_number}`) ? '▾' : '▸'
          }}</span>
          <span class="issue-ref">#{{ node.issue_number }}</span>
          <span class="issue-title">{{ node.title }}</span>
          <span class="state-badge" :class="'badge-' + node.state">{{ node.state }}</span>
          <span class="group-count">{{ countTasks(node.tasks) }}</span>
        </div>

        <div v-if="isExpanded(`${node.issue_repo}#${node.issue_number}`)" class="group-tasks">
          <TaskTreeRow
            v-for="task in node.tasks"
            :key="task.id"
            :task="task"
            :depth="0"
            :selected-task-id="tasksStore.selectedTaskId"
            @open-task="$emit('open-task', $event)"
          />
        </div>
      </div>

      <!-- Ungrouped tasks -->
      <div v-if="treeData!.ungrouped.length" class="tree-group">
        <div class="group-header" @click="toggle('__ungrouped__')">
          <span class="collapse-icon">{{ isExpanded('__ungrouped__') ? '▾' : '▸' }}</span>
          <span class="issue-title ungrouped-label">Ungrouped</span>
          <span class="group-count">{{ countTasks(treeData!.ungrouped) }}</span>
        </div>
        <div v-if="isExpanded('__ungrouped__')" class="group-tasks">
          <TaskTreeRow
            v-for="task in treeData!.ungrouped"
            :key="task.id"
            :task="task"
            :depth="0"
            :selected-task-id="tasksStore.selectedTaskId"
            @open-task="$emit('open-task', $event)"
          />
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useGuildStore } from '../../stores/guild'
import { useTasksStore } from '../../stores/tasks'
import { api } from '../../utils/api'
import type { TaskTreeData, TaskTreeNode } from '../../types'
import TaskTreeRow from './TaskTreeRow.vue'

defineEmits<{ (e: 'open-task', id: string): void }>()

const guildStore = useGuildStore()
const tasksStore = useTasksStore()

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

// Refetch (debounced) when the flat tasks list changes — covers WS task events
watch(
  () => tasksStore.tasks.length,
  () => {
    const guildId = guildStore.currentGuild?.id
    if (guildId) debouncedFetch(guildId)
  },
)

// Also watch for state changes on individual tasks
watch(
  () => tasksStore.tasks.map((t) => t.state).join(','),
  () => {
    const guildId = guildStore.currentGuild?.id
    if (guildId) debouncedFetch(guildId)
  },
)

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function countTasks(tasks: TaskTreeNode[]): number {
  let n = tasks.length
  for (const t of tasks) n += countTasks(t.children)
  return n
}

const isEmpty = computed(
  () => !loading.value && treeData.value && treeData.value.nodes.length === 0 && treeData.value.ungrouped.length === 0,
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

.issue-ref {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-brass);
  letter-spacing: 0.5px;
  flex-shrink: 0;
}

.issue-title {
  font-size: 11px;
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1;
  min-width: 0;
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

.badge-open {
  background: rgba(80, 200, 120, 0.2);
  color: var(--color-green);
}

.badge-closed {
  background: rgba(200, 80, 80, 0.2);
  color: var(--color-red);
}

.badge-merged {
  background: rgba(130, 80, 200, 0.2);
  color: #a07de0;
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
