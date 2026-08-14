<template>
  <div class="thread-detail">
    <div v-if="!thread" class="empty-state">Loading thread…</div>
    <template v-else>
      <div class="pane-header">
        <div class="pane-title">
          <span class="title-icon">✉</span>
          <span class="title-text">{{ thread.name || thread.id }}</span>
          <code class="entity-id-chip">{{ thread.id }}</code>
        </div>
        <div class="pane-meta">
          <span class="status-pill" :class="'status-' + thread.status">
            {{ threadsStore.statusLabel(thread.status) }}
          </span>
        </div>
      </div>

      <div class="pane-subheader">
        <span class="sub-field">conversation #{{ thread.conversation_id }}</span>
        <span v-if="thread.discord_thread_id" class="sub-field discord-chip">
          ⇄ mirrored to Discord thread {{ thread.discord_thread_id }}
        </span>
        <span v-else class="sub-field discord-pending"> no Discord mirror yet </span>
        <span class="sub-time-group">
          <span class="sub-time">created {{ formatRelative(thread.created_at) }}</span>
          <span class="sub-time">updated {{ formatRelative(thread.updated_at) }}</span>
        </span>
      </div>

      <div class="body">
        <p class="hint">
          This thread is a Foreman conversation — Discord is just one surface it can be
          mirrored to, not the source. Use the Foreman comms pane for the full message
          history; below is the task/follow-up/PR activity the Foreman has created while
          handling this conversation.
        </p>

        <h3 class="section-title">Linked Activity</h3>
        <p v-if="linkedTasks.length === 0" class="hint">
          No tasks linked to this thread yet
          <template v-if="hasUnattributedTasks">
            (tasks fetched before this session's WebSocket connection don't carry thread
            links yet)</template
          >.
        </p>
        <ul v-else class="activity-list">
          <li
            v-for="task in linkedTasks"
            :key="task.id"
            class="activity-row"
            @click="openTask(task.id)"
          >
            <span class="activity-state" :class="'state-' + tasksStore.stateColor(task.state)">
              {{ tasksStore.stateLabel(task.state) }}
            </span>
            <span class="activity-name">{{ task.name || task.description || task.id }}</span>
            <a
              v-if="task.pr_url"
              :href="task.pr_url"
              target="_blank"
              rel="noopener"
              class="activity-pr-link"
              @click.stop
            >
              PR
            </a>
          </li>
        </ul>
      </div>

      <div class="actions">
        <button
          v-if="thread.status === 'active'"
          class="pixel-btn"
          :disabled="acting"
          @click="onArchive"
        >
          Archive
        </button>
        <button
          v-if="thread.status !== 'closed'"
          class="pixel-btn close-btn"
          :disabled="acting"
          @click="onClose"
        >
          Close
        </button>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useGuildStore } from '../stores/guild'
import { useThreadsStore } from '../stores/threads'
import { formatRelative } from '../utils/format'

const props = defineProps<{
  id: string
}>()

const guildStore = useGuildStore()
const threadsStore = useThreadsStore()

const acting = ref(false)

const thread = computed(() => threadsStore.threads.find((t) => t.id === props.id))

async function load() {
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  try {
    await threadsStore.fetchThread(guildId, props.id)
  } catch (e) {
    console.error('Failed to fetch thread', e)
  }
}

async function onArchive() {
  const guildId = guildStore.currentGuild?.id
  if (!guildId || acting.value) return
  acting.value = true
  try {
    await threadsStore.archiveThread(guildId, props.id)
  } finally {
    acting.value = false
  }
}

async function onClose() {
  const guildId = guildStore.currentGuild?.id
  if (!guildId || acting.value) return
  acting.value = true
  try {
    await threadsStore.closeThread(guildId, props.id)
  } finally {
    acting.value = false
  }
}

onMounted(() => {
  if (!thread.value) load()
})

watch(() => props.id, load)
</script>

<style scoped>
.thread-detail {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: #080500;
}

.empty-state {
  padding: 24px;
  text-align: center;
  color: var(--color-text-dim);
  font-size: 12px;
  font-style: italic;
}

.pane-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 16px;
  background: #0f0a02;
  border-bottom: 2px solid #2a1a05;
  flex-shrink: 0;
}

.pane-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}

.title-icon {
  color: var(--color-brass);
}

.title-text {
  color: var(--color-text);
}

.entity-id-chip {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--color-text-dim);
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid var(--color-brass-dark);
  padding: 1px 5px;
  border-radius: 2px;
  letter-spacing: 0.5px;
}

.status-pill {
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 1px;
  text-transform: uppercase;
  padding: 2px 6px;
  border-radius: 2px;
}

.status-active {
  background: rgba(80, 200, 120, 0.2);
  color: var(--color-green);
}
.status-archived {
  background: rgba(232, 170, 0, 0.2);
  color: var(--color-amber);
}
.status-closed {
  background: rgba(255, 255, 255, 0.08);
  color: var(--color-text-dim);
}

.pane-subheader {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 6px 16px;
  background: var(--color-bg-secondary);
  border-bottom: 1px solid var(--color-brass-dark);
  flex-shrink: 0;
}

.sub-field {
  font-size: 10px;
  color: var(--color-text-dim);
  font-family: var(--font-mono);
}

.discord-chip {
  color: var(--color-teal);
}

.discord-pending {
  font-style: italic;
}

.sub-time-group {
  display: flex;
  gap: 10px;
  margin-left: auto;
}

.sub-time {
  font-size: 10px;
  color: var(--color-text-dim);
}

.body {
  padding: 12px 16px;
  flex: 1;
  overflow-y: auto;
}

.hint {
  font-size: 11px;
  color: var(--color-text-dim);
  line-height: 1.5;
  margin: 0;
}

.actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 12px 16px;
  border-top: 2px solid var(--color-brass-dark);
  background: var(--color-bg-tertiary);
  flex-shrink: 0;
}

.close-btn {
  background: transparent;
  border-color: var(--color-red);
  color: var(--color-red);
}

.close-btn:hover {
  background: rgba(255, 80, 80, 0.12);
  box-shadow: none;
}
</style>
