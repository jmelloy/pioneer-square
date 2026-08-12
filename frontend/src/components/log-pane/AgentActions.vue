<template>
  <div class="actions-panel">
    <div class="actions-header">ACTIONS</div>
    <div class="run-bar">
      <select v-model="runTool" class="tool-select" :disabled="isRunning" @change="runModel = ''">
        <option value="claude">claude</option>
        <option value="codex">codex</option>
        <option value="pi">pi</option>
      </select>
      <select
        v-if="toolModels.length"
        v-model="runModel"
        class="model-input"
        :disabled="isRunning"
        title="Model (optional)"
      >
        <option value="">{{ modelPlaceholder }}</option>
        <option v-for="m in toolModels" :key="m.id" :value="m.id">{{ m.name }}</option>
      </select>
      <input
        v-else
        v-model="runModel"
        class="model-input"
        :placeholder="modelPlaceholder"
        :disabled="isRunning"
        title="Model (optional)"
      />
      <input
        v-if="runTool === 'pi'"
        v-model="runProvider"
        class="provider-input"
        placeholder="provider"
        :disabled="isRunning"
        title="Provider (anthropic, openai, google…)"
      />
      <input
        v-model="runPrompt"
        class="prompt-input"
        :placeholder="promptPlaceholder"
        :disabled="isRunning"
        @keydown.enter.exact="handleRun"
      />
      <button
        v-if="!isRunning"
        class="pixel-btn run-btn"
        :disabled="!runPrompt.trim()"
        @click="handleRun"
        title="Start interactive task"
      >
        ▶ RUN
      </button>
      <button v-else class="pixel-btn stop-btn" @click="handleStop" title="Stop agent">
        ■ STOP
      </button>
    </div>
    <div v-if="runError" class="action-error">{{ runError }}</div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useAgentsStore } from '../../stores/agents'
import { useModels } from '../../composables/useModels'

const props = defineProps<{ agentId: string; agentState?: string }>()

const agentsStore = useAgentsStore()
const modelsStore = useModels()

const runTool = ref('claude')
const runPrompt = ref('')
const runModel = ref('')
const runProvider = ref('')
const runError = ref('')

const isRunning = computed(() => ['working', 'thinking', 'busy'].includes(props.agentState ?? ''))

const promptPlaceholder = computed(() => `Start an interactive ${runTool.value} task…`)

const modelPlaceholder = computed(() => {
  if (runTool.value === 'claude') return 'model (default)'
  if (runTool.value === 'codex') return 'model (default)'
  return 'model (optional)'
})

const TOOL_PROVIDER: Record<string, string> = {
  claude: 'anthropic',
  codex: 'openai',
}

const toolModels = computed(() => {
  const providerId = TOOL_PROVIDER[runTool.value]
  if (!providerId) return []
  return modelsStore.modelsForProvider(providerId)
})

onMounted(() => {
  modelsStore.loadModels()
})

async function handleRun() {
  if (!runPrompt.value.trim() || isRunning.value) return
  runError.value = ''
  try {
    const result = await agentsStore.runAgent(props.agentId, {
      tool: runTool.value,
      prompt: runPrompt.value.trim(),
      model: runModel.value.trim(),
      provider: runProvider.value.trim(),
    })
    runPrompt.value = ''
    if (result && typeof result === 'object' && 'taskId' in result) {
      agentsStore.openTaskTab(String(result.taskId))
    }
  } catch (e: unknown) {
    runError.value = e instanceof Error ? e.message : String(e)
  }
}

async function handleStop() {
  runError.value = ''
  try {
    await agentsStore.stopAgent(props.agentId)
  } catch (e: unknown) {
    runError.value = e instanceof Error ? e.message : String(e)
  }
}
</script>

<style scoped>
.actions-panel {
  flex-shrink: 0;
  padding: 8px 12px;
  background: #0c0700;
  border-top: 2px solid #2a1a05;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.actions-header {
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 2px;
  color: var(--color-amber);
}

.run-bar {
  display: flex;
  align-items: center;
  gap: 6px;
}

.tool-select {
  background: var(--color-bg);
  border: 2px solid var(--color-brass-dark);
  color: var(--color-amber);
  font-family: var(--font-mono);
  font-size: 11px;
  padding: 5px 6px;
  outline: none;
  cursor: pointer;
  flex-shrink: 0;
}
.tool-select:focus {
  border-color: var(--color-brass);
}

.model-input,
.provider-input {
  background: var(--color-bg);
  border: 2px solid var(--color-brass-dark);
  color: var(--color-text);
  font-family: var(--font-mono);
  font-size: 11px;
  padding: 5px 8px;
  outline: none;
  width: 120px;
  flex-shrink: 0;
}
.model-input:focus,
.provider-input:focus {
  border-color: var(--color-brass);
}
.model-input::placeholder,
.provider-input::placeholder {
  color: var(--color-text-dim);
  font-style: italic;
}

.prompt-input {
  flex: 1;
  background: var(--color-bg);
  border: 2px solid var(--color-brass-dark);
  color: var(--color-text);
  font-family: var(--font-mono);
  font-size: 12px;
  padding: 5px 10px;
  outline: none;
  min-width: 0;
}
.prompt-input:focus {
  border-color: var(--color-brass);
  box-shadow: 0 0 8px rgba(232, 170, 0, 0.25);
}
.prompt-input::placeholder {
  color: var(--color-text-dim);
  font-style: italic;
}
.prompt-input:disabled,
.model-input:disabled,
.provider-input:disabled,
.tool-select:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.run-btn {
  padding: 5px 12px;
  font-size: 10px;
  color: var(--color-green);
  border-color: var(--color-green);
  flex-shrink: 0;
}
.run-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}
.run-btn:not(:disabled):hover {
  background: rgba(0, 255, 136, 0.12);
  box-shadow: 0 0 8px rgba(0, 255, 136, 0.3);
}

.stop-btn {
  padding: 5px 12px;
  font-size: 10px;
  color: var(--color-red);
  border-color: var(--color-red);
  flex-shrink: 0;
  animation: stopPulse 1.2s infinite;
}
.stop-btn:hover {
  background: rgba(255, 51, 51, 0.12);
}
@keyframes stopPulse {
  0%,
  100% {
    box-shadow: 0 0 0 rgba(255, 51, 51, 0);
  }
  50% {
    box-shadow: 0 0 8px rgba(255, 51, 51, 0.4);
  }
}

.action-error {
  padding: 4px 0 0;
  font-size: 11px;
  color: var(--color-red);
}
</style>
