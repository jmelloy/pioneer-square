<template>
  <GuildSpawnDefaults :guild-id="guildId" />

  <div class="foreman-divider">Worker Tools</div>
  <nav class="foreman-tool-tabs">
    <button
      v-for="ft in WORKER_SUBTABS"
      :key="ft.id"
      type="button"
      class="foreman-tool-tab"
      :class="{ active: workerSubTab === ft.id }"
      @click="workerSubTab = ft.id"
    >
      {{ ft.label }}
    </button>
  </nav>

  <!-- General: env vars every worker tool in this guild receives -->
  <template v-if="workerSubTab === 'general'">
    <p class="foreman-hint">
      Every worker tool in this guild inherits these variables. Use the Claude / Pi / Codex tabs to
      override a value for a single tool.
    </p>
    <div class="env-var-list">
      <div class="env-var-row env-var-head">
        <span class="env-var-key">Key</span>
        <span class="env-var-value">Value</span>
        <span class="env-var-spacer"></span>
      </div>
      <div v-for="row in config.workerEnvRows.value" :key="row.id" class="env-var-row">
        <input
          v-model="row.key"
          class="settings-input env-var-key"
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
          @click="config.removeEnvRow(config.workerEnvRows.value, row)"
          title="Remove variable"
        >
          ✕
        </button>
      </div>
      <button
        class="pixel-btn env-var-add-btn"
        @click="config.addEnvRow(config.workerEnvRows.value)"
      >
        + Add Variable
      </button>
    </div>
  </template>

  <!-- Pi: provider-agnostic tool, so it needs a provider + model override -->
  <template v-else-if="workerSubTab === 'pi'">
    <div class="foreman-field">
      <label class="foreman-field-label">Provider Override</label>
      <select v-model="piDefaultProvider" class="settings-input">
        <option value="">default (anthropic)</option>
        <option v-for="p in config.modelsStore.providers" :key="p.id" :value="p.id">
          {{ p.name }}
        </option>
      </select>
    </div>
    <div class="foreman-field">
      <label class="foreman-field-label">Model Override</label>
      <input
        v-model="piDefaultModel"
        class="settings-input"
        list="pi-model-hints"
        :placeholder="
          piDefaultProvider === 'bedrock' ? 'inference-profile ARN' : 'e.g. claude-sonnet-4-6'
        "
        autocomplete="off"
      />
      <datalist id="pi-model-hints">
        <option
          v-for="m in config.piProviderModels.value"
          :key="m.id"
          :value="m.id"
          :label="m.name"
        />
      </datalist>
    </div>
    <p class="foreman-hint">
      Overrides the model Pi runs when the foreman assigns it a task without an explicit
      model/provider. Setting a provider forces Pi onto that provider's models (Pi ignores a bare
      provider unless the model is pinned too). Select "Amazon Bedrock" to run Pi against Bedrock;
      set its AWS credentials in the Pi Environment section below (or the General forwarded
      variables, which apply to every tool).
    </p>
  </template>

  <!-- Codex -->
  <template v-else-if="workerSubTab === 'codex'">
    <div class="foreman-field">
      <label class="foreman-field-label">Default Model</label>
      <input
        v-model="codexDefaultModel"
        class="settings-input"
        placeholder="e.g. gpt-5-codex"
        autocomplete="off"
      />
    </div>
    <p class="foreman-hint">
      Used when the foreman assigns a task to the Codex tool without an explicit model override.
    </p>
  </template>

  <!-- Claude: no per-tool default model here (the foreman's own LLM lives
       in the Foreman tab); only its worker-CLI environment. -->
  <template v-else-if="workerSubTab === 'claude'">
    <p class="foreman-hint">
      The Claude worker CLI takes no default model here — set its override environment below.
    </p>
  </template>

  <!-- Per-tool override environment: passed ONLY to the selected tool's
       runner, never to the other tools; overrides the General vars. -->
  <div v-if="workerSubTab !== 'general'" class="foreman-field">
    <label class="foreman-field-label">{{ activeToolLabel }} Overrides</label>
    <p class="foreman-hint">
      Passed only to the {{ activeToolLabel }} CLI — never to the other tools. Overrides a General
      forwarded variable for this tool. Pick a known variable or type your own.
    </p>
    <div class="env-var-list">
      <div v-for="row in config.toolEnvRows[workerSubTab]" :key="row.id" class="env-var-row">
        <input
          v-model="row.key"
          class="settings-input env-var-key"
          :list="`tool-env-keys-${workerSubTab}`"
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
          @click="config.removeToolEnvVar(workerSubTab, row)"
          title="Remove variable"
        >
          ✕
        </button>
      </div>
      <datalist :id="`tool-env-keys-${workerSubTab}`">
        <option v-for="k in TOOL_ENV_KEYS[workerSubTab]" :key="k" :value="k" />
      </datalist>
      <button class="pixel-btn env-var-add-btn" @click="config.addToolEnvVar(workerSubTab)">
        + Add Variable
      </button>
    </div>
  </div>

  <div class="foreman-actions">
    <button
      class="pixel-btn settings-save-btn"
      :disabled="config.foremanSaving.value"
      @click="config.saveForemanConfig"
    >
      {{ config.foremanSaving.value ? 'Saving…' : 'Save' }}
    </button>
    <span
      v-if="config.foremanStatus.value"
      class="save-status"
      :class="'save-status-' + config.foremanStatus.value"
    >
      {{ config.foremanStatus.value === 'saved' ? 'Saved' : 'Error' }}
    </span>
    <!-- The Save button writes two independent stores. Say which half failed
         instead of one undifferentiated "Error" (issue #1240). -->
    <span v-if="saveDetail" class="save-detail">{{ saveDetail }}</span>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import type { ForemanConfig } from '../composables/useForemanConfig'
import GuildSpawnDefaults from './GuildSpawnDefaults.vue'

const props = defineProps<{ guildId: string; config: ForemanConfig }>()

// Destructured out of the prop (rather than accessed as config.x.value) so
// mutating these refs isn't flagged as a prop mutation — they're shared state
// owned by the composable, not this prop.
const { piDefaultProvider, piDefaultModel, codexDefaultModel } = props.config

const saveDetail = computed(() => {
  const worker = props.config.workerSettingsError.value
  if (worker) return worker
  const foreman = props.config.foremanConfigError.value
  return foreman ? `Worker Settings saved. Foreman config failed: ${foreman}` : ''
})

// Worker Settings sub-tabs: General (vars every tool receives) + one override
// tab per worker tool.
const WORKER_SUBTABS = [
  { id: 'general', label: 'General' },
  { id: 'claude', label: 'Claude' },
  { id: 'pi', label: 'Pi' },
  { id: 'codex', label: 'Codex' },
] as const
const workerSubTab = ref<(typeof WORKER_SUBTABS)[number]['id']>('general')
const activeToolLabel = computed(
  () => WORKER_SUBTABS.find((t) => t.id === workerSubTab.value)?.label ?? '',
)

// Known env-var keys per worker tool, surfaced as datalist suggestions. Users can
// still type any other key. Sourced from each CLI's own docs (`pi --help`, Claude
// Code / Codex provider env vars).
const TOOL_ENV_KEYS: Record<string, string[]> = {
  claude: [
    'ANTHROPIC_API_KEY',
    'CLAUDE_CODE_OAUTH_TOKEN',
    'ANTHROPIC_AUTH_TOKEN',
    'ANTHROPIC_BASE_URL',
    'ANTHROPIC_MODEL',
    'CLAUDE_CODE_USE_BEDROCK',
    'AWS_REGION',
    'AWS_PROFILE',
    'AWS_ACCESS_KEY_ID',
    'AWS_SECRET_ACCESS_KEY',
    'AWS_SESSION_TOKEN',
    'AWS_BEARER_TOKEN_BEDROCK',
  ],
  codex: ['OPENAI_API_KEY', 'OPENAI_BASE_URL'],
  pi: [
    'ANTHROPIC_API_KEY',
    'ANTHROPIC_OAUTH_TOKEN',
    'OPENAI_API_KEY',
    'AZURE_OPENAI_API_KEY',
    'AZURE_OPENAI_BASE_URL',
    'AZURE_OPENAI_RESOURCE_NAME',
    'AZURE_OPENAI_API_VERSION',
    'AZURE_OPENAI_DEPLOYMENT_NAME_MAP',
    'DEEPSEEK_API_KEY',
    'GEMINI_API_KEY',
    'GROQ_API_KEY',
    'CEREBRAS_API_KEY',
    'XAI_API_KEY',
    'FIREWORKS_API_KEY',
    'TOGETHER_API_KEY',
    'OPENROUTER_API_KEY',
    'AI_GATEWAY_API_KEY',
    'ZAI_API_KEY',
    'MISTRAL_API_KEY',
    'MINIMAX_API_KEY',
    'MOONSHOT_API_KEY',
    'OPENCODE_API_KEY',
    'KIMI_API_KEY',
    'CLOUDFLARE_API_KEY',
    'CLOUDFLARE_ACCOUNT_ID',
    'CLOUDFLARE_GATEWAY_ID',
    'XIAOMI_API_KEY',
    'XIAOMI_TOKEN_PLAN_CN_API_KEY',
    'XIAOMI_TOKEN_PLAN_AMS_API_KEY',
    'XIAOMI_TOKEN_PLAN_SGP_API_KEY',
    'AWS_PROFILE',
    'AWS_ACCESS_KEY_ID',
    'AWS_SECRET_ACCESS_KEY',
    'AWS_BEARER_TOKEN_BEDROCK',
    'AWS_REGION',
  ],
}
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

.save-detail {
  font-size: 10px;
  color: var(--color-red);
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

.foreman-tool-tabs {
  display: flex;
  gap: 4px;
  margin-bottom: 2px;
}

.foreman-tool-tab {
  background: none;
  border: 1px solid var(--color-brass-dark);
  color: var(--color-brass-dark);
  cursor: pointer;
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 1px;
  text-transform: uppercase;
  padding: 6px 12px;
  border-radius: 2px;
  transition:
    color 0.12s,
    background 0.12s,
    border-color 0.12s;
}

.foreman-tool-tab:hover {
  color: var(--color-brass);
  background: rgba(232, 170, 0, 0.06);
}

.foreman-tool-tab.active {
  color: var(--color-brass-light);
  background: rgba(232, 170, 0, 0.1);
  border-color: var(--color-brass);
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
  .foreman-tool-tab,
  .env-var-delete-btn {
    transition: none;
  }
}
</style>
