<template>
  <div class="settings-overlay" @mousedown.self="close">
    <div
      class="settings-panel"
      role="dialog"
      aria-modal="true"
      aria-label="User preferences"
      ref="panelRef"
    >
      <header class="settings-panel-header">
        <div class="settings-panel-title">User Preferences</div>
        <div class="settings-panel-user" v-if="authStore.user">
          <span class="settings-panel-user-login">{{ authStore.user.login }}</span>
        </div>
        <button class="settings-close-btn" @click="close" title="Close preferences">✕</button>
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
          <!-- GitHub -->
          <section v-if="activeTab === 'github'" class="settings-section">
            <div class="user-card">
              <img :src="authStore.user?.avatar_url" class="avatar" alt="avatar" />
              <div class="user-info">
                <div class="user-name">{{ authStore.user?.login }}</div>
                <div class="user-label">{{ authStore.user?.name || 'GitHub User' }}</div>
              </div>
            </div>
          </section>

          <!-- Worker Settings — this user's personal spawn defaults for the
               current guild (repos/tools/per-tool model & env overrides). -->
          <section v-else-if="activeTab === 'worker'" class="settings-section">
            <UserWorkerSettings :guild-id="currentGuild?.id" />
          </section>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useAuthStore } from '../stores/auth'
import { useGuildStore } from '../stores/guild'
import UserWorkerSettings from './UserWorkerSettings.vue'

const emit = defineEmits<{ close: [] }>()

const authStore = useAuthStore()
const guildStore = useGuildStore()

const currentGuild = computed(() => guildStore.currentGuild)

const TABS = [
  { id: 'github', label: 'GitHub' },
  { id: 'worker', label: 'Worker Settings' },
] as const
const activeTab = ref<(typeof TABS)[number]['id']>('github')

const panelRef = ref<HTMLElement | null>(null)

function close() {
  emit('close')
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') close()
}

onMounted(() => {
  document.addEventListener('keydown', onKeydown)
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
  z-index: 2000;
  padding: 20px;
}

.settings-panel {
  width: min(720px, 94vw);
  height: min(560px, 88vh);
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

.settings-panel-user {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
  flex: 1;
}

.settings-panel-user-login {
  font-family: var(--font-pixel);
  font-size: 8px;
  color: var(--color-brass);
  letter-spacing: 1px;
  text-transform: uppercase;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
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

.user-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px;
  background: rgba(0, 187, 170, 0.08);
  border: 1px solid rgba(0, 187, 170, 0.3);
  border-left: 3px solid var(--color-teal);
}

.avatar {
  width: 44px;
  height: 44px;
  border-radius: 50%;
  border: 2px solid var(--color-teal);
}

.user-info {
  flex: 1;
}

.user-name {
  font-family: var(--font-pixel);
  font-size: 8px;
  color: var(--color-teal);
  margin-bottom: 4px;
}

.user-label {
  font-size: 11px;
  color: var(--color-text-dim);
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
