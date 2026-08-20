<template>
  <div class="thread-view">
    <div v-if="!thread" class="loading-state">
      <span v-if="loading" class="spinner">⟳</span>
      <span v-else>Conversation not found</span>
    </div>

    <template v-else>
      <!-- Header -->
      <div class="view-header">
        <div class="header-left">
          <button class="back-btn" @click="$emit('back')" title="Back to list">←</button>
          <span class="thread-icon">💬</span>
          <div class="header-info">
            <span class="thread-name">{{ thread.name || 'Unnamed conversation' }}</span>
            <code class="thread-id-chip">{{ thread.id }}</code>
          </div>
        </div>
        <div class="header-right">
          <span class="status-badge" :class="'badge-' + thread.status">
            {{ statusLabel }}
          </span>
        </div>
      </div>

      <!-- Metadata -->
      <div class="view-meta">
        <div class="meta-row">
          <span class="meta-label">Conversation</span>
          <span class="meta-value">#{{ thread.conversation_id }}</span>
        </div>
        <div class="meta-row">
          <span class="meta-label">Discord</span>
          <span v-if="thread.discord_thread_id" class="meta-value discord-linked">
            {{ thread.discord_thread_id }}
          </span>
          <span v-else class="meta-value discord-pending">not yet linked</span>
        </div>
        <div class="meta-row">
          <span class="meta-label">Created</span>
          <span class="meta-value">{{ formatRelative(thread.created_at) }}</span>
        </div>
        <div class="meta-row">
          <span class="meta-label">Updated</span>
          <span class="meta-value">{{ formatRelative(thread.updated_at) }}</span>
        </div>
      </div>

      <!-- Body / conversation placeholder -->
      <div class="view-body">
        <div class="conversation-placeholder">
          <p class="placeholder-text">
            Conversation messages appear here when this panel is wired into the active shell. Use
            the Foreman comms pane for the full conversation history.
          </p>
          <div class="foreman-badge">
            <span class="foreman-icon">⚙</span>
            <span>Managed by Foreman</span>
          </div>
        </div>
      </div>

      <!-- Actions footer -->
      <div class="view-actions">
        <button
          v-if="thread.status === 'active'"
          class="action-btn archive-btn"
          :disabled="acting"
          @click="onArchive"
        >
          {{ acting ? '…' : 'Archive' }}
        </button>
        <button
          v-if="thread.status !== 'closed'"
          class="action-btn close-btn"
          :disabled="acting"
          @click="onClose"
        >
          {{ acting ? '…' : 'Close' }}
        </button>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useThreadsStore } from '../../stores/threads'
import { useGuildStore } from '../../stores/guild'
import { formatRelative } from '../../utils/format'

const props = defineProps<{
  threadId: string
}>()

defineEmits<{
  (e: 'back'): void
}>()

const guildStore = useGuildStore()
const threadsStore = useThreadsStore()

const loading = ref(false)
const acting = ref(false)

const thread = computed(() => threadsStore.threads.find((t) => t.id === props.threadId))
const statusLabel = computed(() =>
  thread.value ? threadsStore.statusLabel(thread.value.status) : '',
)

async function load() {
  const guildId = guildStore.currentGuild?.id
  if (!guildId || thread.value) return
  loading.value = true
  try {
    await threadsStore.fetchThread(guildId, props.threadId)
  } catch (e) {
    console.error('Failed to load conversation', e)
  } finally {
    loading.value = false
  }
}

async function onArchive() {
  const guildId = guildStore.currentGuild?.id
  if (!guildId || acting.value) return
  acting.value = true
  try {
    await threadsStore.archiveThread(guildId, props.threadId)
  } finally {
    acting.value = false
  }
}

async function onClose() {
  const guildId = guildStore.currentGuild?.id
  if (!guildId || acting.value) return
  acting.value = true
  try {
    await threadsStore.closeThread(guildId, props.threadId)
  } finally {
    acting.value = false
  }
}

onMounted(load)
watch(() => props.threadId, load)
</script>

<style scoped>
.thread-view {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
  background: var(--color-bg);
}

.loading-state {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--color-text-dim);
  font-size: 12px;
  font-style: italic;
}

/* Header */
.view-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px;
  background: var(--color-bg-tertiary);
  border-bottom: 2px solid var(--color-brass-dark);
  flex-shrink: 0;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.back-btn {
  font-size: 16px;
  color: var(--color-brass);
  background: transparent;
  border: 1px solid var(--color-brass-dark);
  padding: 2px 8px;
  cursor: pointer;
  transition: all 0.12s;
  line-height: 1;
}

.back-btn:hover {
  background: rgba(232, 170, 0, 0.12);
  border-color: var(--color-brass);
}

.thread-icon {
  font-size: 16px;
  color: var(--color-brass);
}

.header-info {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.thread-name {
  font-size: 13px;
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.thread-id-chip {
  font-family: var(--font-mono);
  font-size: 9px;
  color: var(--color-text-dim);
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid var(--color-brass-dark);
  padding: 1px 5px;
  border-radius: 2px;
  flex-shrink: 0;
}

.header-right {
  flex-shrink: 0;
}

.status-badge {
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 1px;
  text-transform: uppercase;
  padding: 3px 8px;
  border-radius: 2px;
}

.badge-active {
  background: rgba(51, 221, 136, 0.15);
  color: var(--color-green);
}

.badge-archived {
  background: rgba(255, 204, 0, 0.15);
  color: var(--color-amber);
}

.badge-closed {
  background: rgba(255, 255, 255, 0.06);
  color: var(--color-text-dim);
}

/* Metadata */
.view-meta {
  padding: 10px 16px;
  background: var(--color-bg-secondary);
  border-bottom: 1px solid rgba(232, 170, 0, 0.1);
  flex-shrink: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 8px 20px;
}

.meta-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.meta-label {
  font-family: var(--font-pixel);
  font-size: 5px;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  color: var(--color-text-dim);
}

.meta-value {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--color-text);
}

.discord-linked {
  color: var(--color-teal);
}

.discord-pending {
  color: var(--color-text-dim);
  font-style: italic;
  font-size: 10px;
}

/* Body */
.view-body {
  flex: 1;
  overflow-y: auto;
  padding: 20px 16px;
}

.conversation-placeholder {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 16px;
  padding: 32px;
  text-align: center;
}

.placeholder-text {
  font-size: 11px;
  color: var(--color-text-dim);
  line-height: 1.6;
  max-width: 360px;
}

.foreman-badge {
  display: flex;
  align-items: center;
  gap: 6px;
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 1px;
  text-transform: uppercase;
  color: var(--color-brass);
  padding: 6px 12px;
  border: 1px solid var(--color-brass-dark);
  border-radius: 2px;
  background: rgba(232, 170, 0, 0.05);
}

.foreman-icon {
  font-size: 12px;
  animation: gear-spin 4s linear infinite;
}

/* Actions */
.view-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 12px 16px;
  border-top: 2px solid var(--color-brass-dark);
  background: var(--color-bg-tertiary);
  flex-shrink: 0;
}

.action-btn {
  font-family: var(--font-pixel);
  font-size: 7px;
  letter-spacing: 1px;
  text-transform: uppercase;
  padding: 6px 14px;
  background: transparent;
  border: 1px solid var(--color-brass);
  color: var(--color-brass-light);
  cursor: pointer;
  transition: all 0.15s;
}

.action-btn:hover:not(:disabled) {
  background: rgba(232, 170, 0, 0.12);
  box-shadow: 0 0 8px rgba(232, 170, 0, 0.2);
}

.action-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.close-btn {
  border-color: var(--color-red);
  color: var(--color-red);
}

.close-btn:hover:not(:disabled) {
  background: rgba(238, 51, 34, 0.1);
  box-shadow: 0 0 8px rgba(238, 51, 34, 0.2);
}

.spinner {
  animation: spin 1s linear infinite;
  display: inline-block;
  font-size: 16px;
}

@keyframes spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}

@keyframes gear-spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}
</style>
