<template>
  <div class="debug-sidebar-overlay" @click.self="emit('close')">
    <div class="debug-sidebar">
      <div class="debug-sidebar-header">
        <span class="debug-sidebar-title">⚙ FOREMAN DEBUG</span>
        <div class="debug-sidebar-actions">
          <span class="debug-count"
            >{{ debugContext.length }} msg{{ debugContext.length !== 1 ? 's' : '' }} in
            context</span
          >
          <button class="pixel-btn refresh-btn" @click="refreshDebug" :disabled="debugLoading">
            {{ debugLoading ? '...' : '↻' }}
          </button>
          <button class="pixel-btn clear-btn" @click="clearContext" :disabled="debugClearing">
            {{ debugClearing ? '...' : 'CLR CTX' }}
          </button>
          <button class="close-btn" @click="emit('close')" title="Close">✕</button>
        </div>
      </div>

      <div class="debug-messages" ref="debugEl">
        <div v-if="debugContext.length === 0 && !debugLoading" class="debug-empty">
          No context — foreman hasn't run yet.
        </div>
        <div v-if="debugLoading" class="debug-empty">Loading...</div>
        <div
          v-for="(msg, i) in debugContext"
          :key="i"
          class="debug-msg"
          :class="
            msg.role === 'assistant'
              ? 'debug-assistant'
              : isToolResponseMsg(msg)
                ? 'debug-tool-response'
                : 'debug-user'
          "
        >
          <span class="debug-role">{{
            isToolResponseMsg(msg) ? 'TOOL RESPONSE' : msg.role.toUpperCase()
          }}</span>
          <template v-if="typeof msg.content === 'string'">
            <span class="debug-text">{{ msg.content }}</span>
          </template>
          <template v-else>
            <div
              v-for="(block, bi) in msg.content"
              :key="bi"
              class="debug-block"
              :class="`debug-block-${block.type}`"
            >
              <span class="debug-block-type">{{ block.type }}</span>
              <template v-if="block.type === 'text'">
                <span class="debug-text">{{ block.text }}</span>
              </template>
              <template v-else-if="block.type === 'tool_use'">
                <span class="debug-tool-name">{{ block.name }}</span>
                <pre class="debug-pre">{{ JSON.stringify(block.input, null, 2) }}</pre>
              </template>
              <template v-else-if="block.type === 'tool_result'">
                <span class="debug-tool-id">id:{{ block.tool_use_id?.slice(-6) }}</span>
                <pre class="debug-pre">{{
                  typeof block.content === 'string' ? block.content : JSON.stringify(block.content)
                }}</pre>
              </template>
              <template v-else>
                <pre class="debug-pre">{{ JSON.stringify(block) }}</pre>
              </template>
            </div>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, nextTick, onMounted } from 'vue'
import { useGuildStore } from '../stores/guild'
import { api } from '../utils/api'

const emit = defineEmits<{ close: [] }>()

const guildStore = useGuildStore()

interface DebugBlock {
  type: string
  text?: string
  name?: string
  input?: unknown
  tool_use_id?: string
  content?: unknown
}

interface DebugMessage {
  role: string
  content: string | DebugBlock[]
}

const debugContext = ref<DebugMessage[]>([])
const debugLoading = ref(false)
const debugClearing = ref(false)
const debugEl = ref<HTMLElement | null>(null)

function isToolResponseMsg(msg: { role: string; content: unknown }) {
  return (
    msg.role === 'user' &&
    Array.isArray(msg.content) &&
    (msg.content as { type: string }[]).every((b) => b.type === 'tool_result')
  )
}

async function refreshDebug() {
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  debugLoading.value = true
  try {
    const data = await api<{ messages?: DebugMessage[] }>(`/guilds/${guildId}/foreman/context`)
    debugContext.value = data?.messages ?? []
  } catch (e) {
    console.error('Failed to load foreman context', e)
  } finally {
    debugLoading.value = false
    await nextTick()
    if (debugEl.value) debugEl.value.scrollTop = debugEl.value.scrollHeight
  }
}

async function clearContext() {
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  debugClearing.value = true
  try {
    await api(`/guilds/${guildId}/foreman/clear-context`, { method: 'POST' })
    debugContext.value = []
  } catch (e) {
    console.error('Failed to clear foreman context', e)
  } finally {
    debugClearing.value = false
  }
}

onMounted(() => refreshDebug())
</script>

<style scoped>
.debug-sidebar-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.4);
  z-index: 300;
  display: flex;
  justify-content: flex-end;
}

.debug-sidebar {
  width: 520px;
  max-width: 90vw;
  height: 100%;
  background: var(--color-bg-secondary);
  border-left: 2px solid var(--color-brass-dark);
  display: flex;
  flex-direction: column;
  box-shadow: -4px 0 24px rgba(0, 0, 0, 0.5);
}

.debug-sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  background: var(--color-bg-tertiary);
  border-bottom: 2px solid var(--color-brass-dark);
  flex-shrink: 0;
  gap: 10px;
}

.debug-sidebar-title {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-brass-light);
  letter-spacing: 2px;
  text-shadow: 0 0 6px rgba(255, 214, 68, 0.4);
  white-space: nowrap;
}

.debug-sidebar-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}

.debug-count {
  font-size: 10px;
  color: var(--color-text-dim);
}

.refresh-btn {
  font-size: 10px;
  padding: 3px 7px;
}

.refresh-btn:disabled {
  opacity: 0.4;
  pointer-events: none;
}

.clear-btn {
  font-size: 7px;
  padding: 3px 6px;
  border-color: var(--color-red, #c0392b);
  color: var(--color-red, #c0392b);
}

.clear-btn:hover {
  background: rgba(192, 57, 43, 0.15);
}

.clear-btn:disabled {
  opacity: 0.4;
  pointer-events: none;
}

.close-btn {
  background: none;
  border: 1px solid var(--color-brass-dark);
  color: var(--color-text-dim);
  cursor: pointer;
  width: 26px;
  height: 26px;
  border-radius: 2px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  transition:
    border-color 0.12s,
    color 0.12s;
}

.close-btn:hover {
  border-color: var(--color-brass);
  color: var(--color-text);
}

.debug-messages {
  flex: 1;
  overflow-y: auto;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-family: var(--font-mono);
  font-size: 11px;
}

.debug-empty {
  color: var(--color-text-dim);
  font-size: 11px;
  text-align: center;
  margin-top: 20px;
  font-style: italic;
}

.debug-msg {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 6px 10px;
  border-radius: 2px;
}

.debug-assistant {
  background: rgba(0, 187, 170, 0.06);
  border-left: 3px solid var(--color-teal);
}

.debug-user {
  background: rgba(232, 170, 0, 0.06);
  border-left: 3px solid var(--color-brass-dark);
}

.debug-tool-response {
  background: rgba(80, 200, 120, 0.06);
  border-left: 3px solid #50c878;
}

.debug-role {
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 1px;
  color: var(--color-text-dim);
  margin-bottom: 2px;
}

.debug-assistant .debug-role {
  color: var(--color-teal);
}
.debug-user .debug-role {
  color: var(--color-brass);
}
.debug-tool-response .debug-role {
  color: #50c878;
}

.debug-text {
  color: var(--color-text);
  line-height: 1.4;
  word-break: break-word;
  white-space: pre-wrap;
}

.debug-block {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 3px 5px;
  border-radius: 2px;
  margin-top: 2px;
}

.debug-block-tool_use {
  background: rgba(255, 140, 0, 0.1);
  border: 1px solid rgba(255, 140, 0, 0.25);
}

.debug-block-tool_result {
  background: rgba(80, 200, 120, 0.07);
  border: 1px solid rgba(80, 200, 120, 0.2);
}

.debug-block-type {
  font-family: var(--font-pixel);
  font-size: 5px;
  letter-spacing: 1px;
  color: var(--color-text-dim);
  text-transform: uppercase;
}

.debug-block-tool_use .debug-block-type {
  color: #ff8c00;
}
.debug-block-tool_result .debug-block-type {
  color: #50c878;
}

.debug-tool-name {
  font-weight: bold;
  color: #ff8c00;
  font-size: 12px;
}

.debug-tool-id {
  font-size: 9px;
  color: #50c878;
}

.debug-pre {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--color-text-dim);
  font-size: 10px;
  max-height: 120px;
  overflow-y: auto;
}
</style>
