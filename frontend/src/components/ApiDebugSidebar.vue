<template>
  <div class="debug-sidebar-overlay" @click.self="emit('close')">
    <div class="debug-sidebar">
      <div class="debug-sidebar-header">
        <span class="debug-sidebar-title">⇄ REST DEBUG</span>
        <div class="debug-sidebar-actions">
          <span class="debug-count">
            {{ log.pendingCount }} pending · {{ log.failedCount }} failed
            <template v-if="log.avgDurationMs !== null"> · avg {{ log.avgDurationMs }}ms</template>
          </span>
          <button class="pixel-btn clear-btn" @click="log.clear()">CLR</button>
          <button class="close-btn" @click="emit('close')" title="Close">✕</button>
        </div>
      </div>

      <div class="debug-body">
        <section class="section">
          <div class="section-title">TRY A REQUEST</div>
          <form class="try-form" @submit.prevent="send">
            <select v-model="method" class="try-method">
              <option v-for="m in METHODS" :key="m" :value="m">{{ m }}</option>
            </select>
            <input
              v-model="path"
              class="try-path"
              placeholder="/guilds"
              spellcheck="false"
              autocomplete="off"
            />
            <button class="pixel-btn" type="submit" :disabled="sending || !pathValid">
              {{ sending ? '...' : 'SEND' }}
            </button>
          </form>
          <textarea
            v-if="method !== 'GET' && method !== 'DELETE'"
            v-model="body"
            class="try-body"
            placeholder='JSON body, e.g. {"name": "x"}'
            rows="3"
            spellcheck="false"
          />
          <div v-if="!pathValid && path" class="try-error">Path must start with "/".</div>
          <div v-if="resultError" class="try-error">{{ resultError }}</div>
          <pre v-if="result !== null" class="debug-pre">{{ result }}</pre>
        </section>

        <section class="section">
          <div class="section-title">ENDPOINTS ({{ log.endpoints.length }})</div>
          <div v-if="!log.endpoints.length" class="debug-empty">No requests yet.</div>
          <div v-for="e in log.endpoints" :key="e.key" class="endpoint-row">
            <span class="endpoint-key">{{ e.key }}</span>
            <span class="endpoint-count" :class="{ failed: e.failed > 0 }">
              ×{{ e.count }}<template v-if="e.failed"> ({{ e.failed }} failed)</template>
            </span>
          </div>
        </section>

        <section class="section">
          <div class="section-title">RECENT CALLS</div>
          <div v-if="!log.calls.length" class="debug-empty">No requests yet.</div>
          <div
            v-for="c in log.calls"
            :key="c.id"
            class="call-row"
            :class="`call-${c.status}`"
            :title="c.error ?? undefined"
          >
            <span class="call-status">{{ statusLabel(c) }}</span>
            <span class="call-method">{{ c.method }}</span>
            <span class="call-path">{{ c.path }}</span>
            <span class="call-time">{{ c.durationMs !== null ? `${c.durationMs}ms` : '…' }}</span>
          </div>
        </section>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useApiLogStore, type ApiCall } from '../stores/apiLog'
import { api } from '../utils/api'

const emit = defineEmits<{ close: [] }>()

const METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']

const log = useApiLogStore()

const method = ref('GET')
const path = ref('')
const body = ref('')
const sending = ref(false)
const result = ref<string | null>(null)
const resultError = ref<string | null>(null)

// Relative paths only: api() prepends API_BASE and attaches the auth header, so an
// absolute URL here would never be what the user meant.
const pathValid = computed(() => path.value.startsWith('/') && !path.value.startsWith('//'))

function statusLabel(c: ApiCall) {
  if (c.status === 'pending') return '…'
  return c.httpStatus !== null ? String(c.httpStatus) : 'ERR'
}

async function send() {
  if (!pathValid.value || sending.value) return
  resultError.value = null
  result.value = null
  let json: unknown
  if (body.value.trim() && method.value !== 'GET' && method.value !== 'DELETE') {
    try {
      json = JSON.parse(body.value)
    } catch {
      resultError.value = 'Body is not valid JSON.'
      return
    }
  }
  sending.value = true
  try {
    const data = await api(path.value, { method: method.value, json })
    result.value = data === undefined ? '(empty response)' : JSON.stringify(data, null, 2)
  } catch (e) {
    resultError.value = e instanceof Error ? e.message : String(e)
  } finally {
    sending.value = false
  }
}
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

.clear-btn {
  font-size: 7px;
  padding: 3px 6px;
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

.debug-body {
  flex: 1;
  overflow-y: auto;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  font-family: var(--font-mono);
  font-size: 11px;
}

.section {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.section-title {
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 1px;
  color: var(--color-brass);
  margin-bottom: 2px;
}

.debug-empty {
  color: var(--color-text-dim);
  font-style: italic;
}

.try-form {
  display: flex;
  gap: 6px;
}

.try-method,
.try-path,
.try-body {
  background: var(--color-bg-primary, #111);
  border: 1px solid var(--color-brass-dark);
  color: var(--color-text);
  font-family: var(--font-mono);
  font-size: 11px;
  padding: 4px 6px;
  border-radius: 2px;
}

.try-path {
  flex: 1;
  min-width: 0;
}

.try-body {
  resize: vertical;
}

.try-error {
  color: var(--color-red, #c0392b);
}

.debug-pre {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--color-text-dim);
  font-size: 10px;
  max-height: 240px;
  overflow-y: auto;
  padding: 6px 8px;
  background: rgba(80, 200, 120, 0.06);
  border-left: 3px solid #50c878;
}

.endpoint-row,
.call-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 3px 6px;
  border-radius: 2px;
}

.endpoint-row {
  justify-content: space-between;
  background: rgba(232, 170, 0, 0.06);
  border-left: 3px solid var(--color-brass-dark);
}

.endpoint-key,
.call-path {
  color: var(--color-text);
  word-break: break-all;
  min-width: 0;
}

.call-path {
  flex: 1;
}

.endpoint-count,
.call-time {
  color: var(--color-text-dim);
  white-space: nowrap;
}

.endpoint-count.failed {
  color: var(--color-red, #c0392b);
}

.call-status {
  min-width: 28px;
  font-weight: bold;
}

.call-method {
  color: var(--color-text-dim);
  min-width: 44px;
}

.call-ok {
  background: rgba(80, 200, 120, 0.06);
  border-left: 3px solid #50c878;
}
.call-ok .call-status {
  color: #50c878;
}

.call-failed {
  background: rgba(192, 57, 43, 0.1);
  border-left: 3px solid var(--color-red, #c0392b);
}
.call-failed .call-status {
  color: var(--color-red, #c0392b);
}

.call-pending {
  background: rgba(0, 187, 170, 0.06);
  border-left: 3px solid var(--color-teal);
}
.call-pending .call-status {
  color: var(--color-teal);
}
</style>
