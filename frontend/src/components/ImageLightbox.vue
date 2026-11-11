<template>
  <Teleport to="body">
    <div
      v-if="lightboxImage"
      class="lightbox"
      role="dialog"
      aria-modal="true"
      :aria-label="lightboxImage.name"
      @click.self="closeLightbox"
    >
      <div class="lightbox-bar">
        <span class="lightbox-name">{{ lightboxImage.name }}</span>
        <a
          class="pixel-btn lightbox-btn"
          :href="lightboxImage.url"
          :download="lightboxImage.name"
          target="_blank"
          rel="noopener noreferrer"
          >Download</a
        >
        <button class="pixel-btn lightbox-btn" aria-label="Close" @click="closeLightbox">✕</button>
      </div>
      <img class="lightbox-img" :src="lightboxImage.url" :alt="lightboxImage.name" />
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { watch, onBeforeUnmount } from 'vue'
import { lightboxImage, closeLightbox } from '../composables/useLightbox'

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') closeLightbox()
}

watch(
  lightboxImage,
  (img) => {
    if (img) window.addEventListener('keydown', onKey)
    else window.removeEventListener('keydown', onKey)
  },
  { immediate: true },
)
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>

<style scoped>
.lightbox {
  position: fixed;
  inset: 0;
  z-index: 1000;
  background: rgba(0, 0, 0, 0.85);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 12px;
  gap: 10px;
}
.lightbox-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  max-width: 1200px;
}
.lightbox-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--font-mono);
  color: var(--color-text);
}
.lightbox-btn {
  padding: 6px 10px;
  font-size: 10px;
  text-decoration: none;
}
.lightbox-img {
  max-width: 100%;
  max-height: calc(100vh - 80px);
  object-fit: contain;
  border: 2px solid var(--color-brass-dark);
}
</style>
