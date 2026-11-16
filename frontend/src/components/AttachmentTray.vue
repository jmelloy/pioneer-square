<template>
  <div v-if="attachments.length || notice" class="attachment-tray">
    <div v-if="notice" class="tray-notice" role="status">{{ notice }}</div>
    <div v-if="attachments.length" class="tray-items">
      <div v-for="a in attachments" :key="a.key" class="tray-item" :class="a.status">
        <img class="tray-thumb" :src="a.previewUrl" :alt="a.file.name" />
        <button
          type="button"
          class="tray-remove"
          :aria-label="'Remove ' + a.file.name"
          @click="emit('remove', a.key)"
        >
          ✕
        </button>
        <div
          v-if="a.status === 'uploading'"
          class="tray-progress"
          role="progressbar"
          :aria-label="'Uploading ' + a.file.name"
          :aria-valuenow="Math.round(a.progress * 100)"
          :aria-valuemin="0"
          :aria-valuemax="100"
        >
          <div
            class="tray-progress-bar"
            :style="{ width: Math.round(a.progress * 100) + '%' }"
          ></div>
        </div>
        <button
          v-else-if="a.status === 'error'"
          type="button"
          class="tray-retry"
          :title="a.error ?? 'Upload failed'"
          @click="emit('retry', a.key)"
        >
          ⟳ Retry
        </button>
        <div v-if="a.status === 'error'" class="tray-error" role="status">{{ a.error }}</div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { Attachment } from '../composables/useAttachments'

defineProps<{ attachments: Attachment[]; notice?: string | null }>()
const emit = defineEmits<{ (e: 'remove', key: number): void; (e: 'retry', key: number): void }>()
</script>

<style scoped>
.attachment-tray {
  max-height: 30dvh;
  overflow-y: auto;
  padding: 8px 12px 0;
  border-top: 2px solid var(--color-brass-dark);
  flex-shrink: 0;
}
.tray-notice,
.tray-error {
  font: var(--text-small) / var(--leading-body) var(--font-ui);
  color: var(--color-red);
  overflow-wrap: anywhere;
}
.tray-items {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.tray-item {
  position: relative;
  width: 112px;
  min-width: 0;
  max-width: 100%;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.tray-thumb {
  width: 100%;
  height: 80px;
  object-fit: cover;
  border: 2px solid var(--color-brass-dark);
  background: var(--color-bg);
}
.tray-item.error .tray-thumb {
  border-color: var(--color-red);
}
.tray-item.uploading .tray-thumb {
  opacity: 0.6;
}
.tray-remove {
  position: absolute;
  top: 2px;
  right: 2px;
  width: 44px;
  height: 44px;
  font: var(--text-card-title) / 1 var(--font-ui);
  border: 1px solid var(--color-brass-dark);
  background: rgba(0, 0, 0, 0.75);
  color: var(--color-text);
  cursor: pointer;
}
.tray-progress {
  height: 4px;
  background: var(--color-bg);
  border: 1px solid var(--color-brass-dark);
}
.tray-progress-bar {
  height: 100%;
  background: var(--color-brass);
  transition: width 0.1s linear;
}
.tray-retry {
  font: var(--text-small) / 1.4 var(--font-ui);
  min-height: 44px;
  background: transparent;
  border: 1px solid var(--color-red);
  color: var(--color-red);
  cursor: pointer;
}
</style>
