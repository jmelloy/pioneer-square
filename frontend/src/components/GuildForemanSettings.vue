<template>
  <p class="foreman-hint">
    These configure the foreman's own orchestrator LLM (the AI itself). Fields left blank fall
    back to the server defaults shown below.
  </p>
  <div class="foreman-field">
    <label class="foreman-field-label">Provider</label>
    <select v-model="foremanProvider" class="settings-input" @change="onProviderChange">
      <option value="">{{ providerDefaultLabel }}</option>
      <option v-for="p in modelsStore.providers" :key="p.id" :value="p.id">
        {{ p.name }}
      </option>
    </select>
  </div>
  <div class="foreman-field">
    <label class="foreman-field-label">Model</label>
    <input
      v-model="foremanModel"
      class="settings-input"
      list="foreman-model-hints"
      :placeholder="foremanModelPlaceholder"
      autocomplete="off"
    />
    <datalist id="foreman-model-hints">
      <option v-for="m in foremanProviderModels" :key="m.id" :value="m.id" :label="m.name" />
    </datalist>
  </div>

  <div class="foreman-divider">Orchestration</div>

  <div class="foreman-field">
    <label class="foreman-field-label">System Prompt Suffix</label>
    <textarea
      v-model="foremanSystemSuffix"
      class="settings-input foreman-textarea"
      placeholder="Additional instructions appended to the system prompt…"
      rows="3"
    />
  </div>

  <div class="foreman-field foreman-row">
    <div class="foreman-half">
      <label class="foreman-field-label">Max Rounds</label>
      <input
        v-model.number="foremanMaxRounds"
        class="settings-input"
        type="number"
        min="1"
        max="50"
        placeholder="10"
      />
    </div>
    <div class="foreman-half">
      <label class="foreman-field-label">Poll Min (s)</label>
      <input
        v-model.number="foremanPollMin"
        class="settings-input"
        type="number"
        min="10"
        placeholder="60"
      />
    </div>
    <div class="foreman-half">
      <label class="foreman-field-label">Poll Max (s)</label>
      <input
        v-model.number="foremanPollMax"
        class="settings-input"
        type="number"
        min="60"
        placeholder="3600"
      />
    </div>
  </div>

  <div class="foreman-field">
    <label class="foreman-field-label">Environment Variables</label>
    <p class="foreman-hint">
      Used only by the foreman's own LLM (credentials, base URLs, etc.) — these are
      <em>not</em> sent to workers. To give a variable to every worker, add it under Worker
      Settings → General; to scope it to one tool, use the Claude / Pi / Codex tabs.
    </p>
    <p v-if="envDefaultKeys.length" class="foreman-hint">
      From the server environment (masked, always available to the foreman):
      {{ envDefaultsSummary }}
    </p>
    <div class="env-var-list">
      <div class="env-var-row env-var-head">
        <span class="env-var-key">Key</span>
        <span class="env-var-value">Value</span>
        <span class="env-var-spacer"></span>
      </div>
      <div v-for="row in foremanEnvRows" :key="row.id" class="env-var-row">
        <input
          v-model="row.key"
          class="settings-input env-var-key"
          list="foreman-env-keys"
          placeholder="KEY_NAME"
          spellcheck="false"
          autocomplete="off"
        />
        <input
          v-model="row.value"
          type="text"
          class="settings-input env-var-value"
          placeholder="value"
          spellcheck="false"
          autocomplete="off"
        />
        <button
          class="env-var-delete-btn"
          @click="removeEnvRow(foremanEnvRows, row)"
          title="Remove variable"
        >
          ✕
        </button>
      </div>
      <datalist id="foreman-env-keys">
        <option v-for="k in FOREMAN_ENV_KEYS" :key="k" :value="k" />
      </datalist>
      <button class="pixel-btn env-var-add-btn" @click="addEnvRow(foremanEnvRows)">
        + Add Variable
      </button>
    </div>
  </div>

  <div class="foreman-actions">
    <button class="pixel-btn settings-save-btn" :disabled="foremanSaving" @click="saveForemanConfig">
      {{ foremanSaving ? 'Saving…' : 'Save' }}
    </button>
    <span v-if="foremanStatus" class="save-status" :class="'save-status-' + foremanStatus">
      {{ foremanStatus === 'saved' ? 'Saved' : 'Error' }}
    </span>
  </div>
</template>

<script setup lang="ts">
import type { ForemanConfig } from '../composables/useForemanConfig'

const props = defineProps<{ config: ForemanConfig }>()

// Destructured out of the prop (rather than accessed as config.x.value) so the
// template reads cleanly and mutating these refs isn't flagged as a prop
// mutation — they're shared state owned by the composable, not this prop.
const {
  modelsStore,
  foremanModel,
  foremanProvider,
  onProviderChange,
  foremanProviderModels,
  foremanSystemSuffix,
  foremanMaxRounds,
  foremanPollMin,
  foremanPollMax,
  foremanSaving,
  foremanStatus,
  envDefaultKeys,
  envDefaultsSummary,
  providerDefaultLabel,
  foremanModelPlaceholder,
  foremanEnvRows,
  addEnvRow,
  removeEnvRow,
  saveForemanConfig,
} = props.config

// Common foreman env-var keys, surfaced as datalist suggestions on the Foreman
// tab. Users can still type any other key.
const FOREMAN_ENV_KEYS = [
  'ANTHROPIC_API_KEY',
  'ANTHROPIC_AUTH_TOKEN',
  'ANTHROPIC_BASE_URL',
  'AWS_DEFAULT_REGION',
  'AWS_PROFILE',
  'AWS_ACCESS_KEY_ID',
  'AWS_SECRET_ACCESS_KEY',
  'AWS_SESSION_TOKEN',
  'AWS_BEARER_TOKEN_BEDROCK',
]
</script>

<style scoped>
.settings-input {
  flex: 1;
  background: var(--color-bg);
  border: 1px solid var(--color-brass-dark);
  color: var(--color-text);
  font-family: var(--font-mono, monospace);
  font-size: 11px;
  padding: 5px 7px;
  outline: none;
  border-radius: 2px;
  min-width: 0;
}

.settings-input:focus {
  border-color: var(--color-brass);
}

.settings-save-btn {
  font-size: 7px;
  padding: 5px 9px;
  flex-shrink: 0;
}

.settings-save-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.save-status {
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 0.5px;
  white-space: nowrap;
  flex-shrink: 0;
}

.save-status-saved {
  color: var(--color-green);
}
.save-status-error {
  color: var(--color-red);
}

.foreman-divider {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-brass);
  letter-spacing: 1px;
  text-transform: uppercase;
  border-top: 1px solid var(--color-brass-dark);
  padding-top: 12px;
  margin-top: 6px;
}

.foreman-field {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.foreman-field-label {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-brass-dark);
  letter-spacing: 1px;
  text-transform: uppercase;
}

.foreman-hint {
  font-family: var(--font-mono, monospace);
  font-size: 9px;
  color: var(--color-text-dim);
  font-style: italic;
  margin: 0 0 3px;
  line-height: 1.4;
}

.foreman-textarea {
  resize: vertical;
  min-height: 60px;
  font-family: var(--font-mono, monospace);
  font-size: 10px;
}

.foreman-row {
  flex-direction: row;
  gap: 8px;
  align-items: flex-start;
}

.foreman-half {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.foreman-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 2px;
}

.env-var-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.env-var-row {
  display: flex;
  align-items: center;
  gap: 4px;
}

.env-var-key {
  flex: 0 0 160px;
  min-width: 0;
  font-size: 10px;
}

.env-var-value {
  flex: 1;
  min-width: 0;
  font-size: 10px;
}

.env-var-spacer {
  flex: 0 0 22px;
}

.env-var-head span {
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-brass-dark);
  letter-spacing: 1px;
  text-transform: uppercase;
}

.env-var-delete-btn {
  background: none;
  border: 1px solid var(--color-brass-dark);
  color: var(--color-text-dim);
  cursor: pointer;
  width: 22px;
  height: 22px;
  border-radius: 2px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  flex-shrink: 0;
  transition:
    border-color 0.12s,
    color 0.12s;
}

.env-var-delete-btn:hover {
  border-color: var(--color-red, #c0392b);
  color: var(--color-red, #c0392b);
}

.env-var-add-btn {
  font-size: 7px;
  padding: 4px 8px;
  align-self: flex-start;
  margin-top: 2px;
}

@media (prefers-reduced-motion: reduce) {
  .env-var-delete-btn {
    transition: none;
  }
}
</style>
