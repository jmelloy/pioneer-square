<template>
  <div class="settings-overlay" @mousedown.self="close">
    <div
      class="settings-panel"
      role="dialog"
      aria-modal="true"
      aria-label="Guild settings"
      ref="panelRef"
    >
      <header class="settings-panel-header">
        <div class="settings-panel-title">Guild Settings</div>
        <div class="settings-panel-guild" v-if="currentGuild">
          <span class="settings-panel-guild-name">{{ currentGuild.name || 'Unnamed Guild' }}</span>
          <span class="settings-panel-guild-id">#{{ currentGuild.id }}</span>
        </div>
        <button class="settings-close-btn" @click="close" title="Close settings">✕</button>
      </header>

      <div class="settings-panel-body">
        <nav class="settings-tabs">
          <button
            v-for="t in TABS"
            :key="t.id"
            class="settings-tab"
            :class="{ active: activeTab === t.id }"
            @click="activeTab = t.id"
          >
            {{ t.label }}
          </button>
        </nav>

        <div class="settings-content">
          <!-- General -->
          <section v-if="activeTab === 'general'" class="settings-section">
            <GuildGeneralSettings />
          </section>

          <!-- Foreman — the orchestrator AI itself (its LLM + orchestration knobs).
               Per-worker-tool defaults/env live under Worker Settings. -->
          <section v-else-if="activeTab === 'foreman'" class="settings-section">
            <GuildForemanSettings :config="foremanConfig" />
          </section>

          <!-- Worker Settings — spawn defaults + per-worker-tool model/env config -->
          <section v-else-if="activeTab === 'spawn'" class="settings-section">
            <GuildWorkerSettings v-if="currentGuild" :guild-id="currentGuild.id" :config="foremanConfig" />
          </section>

          <!-- Members -->
          <section v-else-if="activeTab === 'members'" class="settings-section">
            <GuildMembers v-if="currentGuild" :guild-id="currentGuild.id" />
          </section>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useGuildStore } from '../stores/guild'
import { useForemanConfig } from '../composables/useForemanConfig'
import GuildGeneralSettings from './GuildGeneralSettings.vue'
import GuildForemanSettings from './GuildForemanSettings.vue'
import GuildWorkerSettings from './GuildWorkerSettings.vue'
import GuildMembers from './GuildMembers.vue'

const emit = defineEmits<{ close: [] }>()

const guildStore = useGuildStore()

const currentGuild = computed(() => guildStore.currentGuild)

const TABS = [
  { id: 'general', label: 'General' },
  { id: 'foreman', label: 'Foreman' },
  { id: 'spawn', label: 'Worker Settings' },
  { id: 'members', label: 'Members' },
] as const
const activeTab = ref<(typeof TABS)[number]['id']>('general')

const panelRef = ref<HTMLElement | null>(null)

// Shared between the Foreman tab and the Worker Settings tab: both read and
// write the same guild foreman-config (orchestrator LLM + per-tool defaults).
const foremanConfig = useForemanConfig(computed(() => currentGuild.value?.id))

function close() {
  emit('close')
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') close()
}

onMounted(() => {
  document.addEventListener('keydown', onKeydown)
  foremanConfig.loadModels()
  foremanConfig.loadForemanConfig()
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown)
})
</script>

<style scoped>
.settings-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  /* FactoryFloor robots/stations use z-index up to ~1000 (Math.round(y * 1000)),
     so the settings overlay must sit above that to stay clickable. */
  z-index: 2000;
  padding: 20px;
}

.settings-panel {
  width: min(920px, 94vw);
  height: min(620px, 88vh);
  background: var(--color-bg-secondary);
  border: 2px solid var(--color-brass);
  box-shadow:
    0 6px 24px rgba(0, 0, 0, 0.5),
    0 0 16px rgba(232, 170, 0, 0.15);
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.settings-panel-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 14px;
  border-bottom: 2px solid var(--color-brass-dark);
  flex-shrink: 0;
}

.settings-panel-title {
  font-family: var(--font-pixel);
  font-size: 9px;
  color: var(--color-brass-light);
  letter-spacing: 2px;
  text-transform: uppercase;
  flex-shrink: 0;
}

.settings-panel-guild {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
  flex: 1;
}

.settings-panel-guild-name {
  font-family: var(--font-pixel);
  font-size: 8px;
  color: var(--color-brass);
  letter-spacing: 1px;
  text-transform: uppercase;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.settings-panel-guild-id {
  font-family: var(--font-mono, monospace);
  font-size: 10px;
  color: var(--color-text-dim);
  flex-shrink: 0;
}

.settings-close-btn {
  background: var(--color-bg);
  border: 1px solid var(--color-brass-dark);
  color: var(--color-brass);
  cursor: pointer;
  width: 28px;
  height: 28px;
  border-radius: 2px;
  font-size: 12px;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition:
    border-color 0.12s,
    background 0.12s,
    color 0.12s;
}

.settings-close-btn:hover {
  border-color: var(--color-brass);
  background: rgba(232, 170, 0, 0.1);
  color: var(--color-brass-light);
}

.settings-panel-body {
  display: flex;
  flex: 1;
  min-height: 0;
}

.settings-tabs {
  display: flex;
  flex-direction: column;
  gap: 2px;
  width: 170px;
  flex-shrink: 0;
  padding: 10px 8px;
  border-right: 1px solid var(--color-brass-dark);
  background: var(--color-bg);
  overflow-y: auto;
}

.settings-tab {
  background: none;
  border: 1px solid transparent;
  color: var(--color-brass-dark);
  cursor: pointer;
  font-family: var(--font-pixel);
  font-size: 7px;
  letter-spacing: 1px;
  text-transform: uppercase;
  text-align: left;
  padding: 9px 10px;
  border-radius: 2px;
  transition:
    color 0.12s,
    background 0.12s,
    border-color 0.12s;
}

.settings-tab:hover {
  color: var(--color-brass);
  background: rgba(232, 170, 0, 0.06);
}

.settings-tab.active {
  color: var(--color-brass-light);
  background: rgba(232, 170, 0, 0.1);
  border-color: var(--color-brass-dark);
}

.settings-content {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
  padding: 16px 18px;
}

.settings-section {
  display: flex;
  flex-direction: column;
  gap: 16px;
  max-width: 560px;
}

@media (max-width: 720px) {
  .settings-overlay {
    padding: 0;
  }

  .settings-panel {
    width: 100vw;
    height: 100dvh;
    max-height: 100dvh;
    border: none;
  }

  .settings-panel-body {
    flex-direction: column;
  }

  .settings-tabs {
    flex-direction: row;
    width: auto;
    gap: 4px;
    border-right: none;
    border-bottom: 1px solid var(--color-brass-dark);
    overflow-x: auto;
    overflow-y: hidden;
  }

  .settings-tab {
    white-space: nowrap;
    flex-shrink: 0;
  }
}

@media (prefers-reduced-motion: reduce) {
  .settings-close-btn,
  .settings-tab {
    transition: none;
  }
}
</style>
