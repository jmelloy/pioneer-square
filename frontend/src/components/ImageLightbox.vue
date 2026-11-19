<template>
  <Teleport to="body">
    <dialog
      v-if="lightboxImage"
      ref="dialog"
      class="lightbox ui-surface"
      :aria-label="lightboxImage.name"
      @click.self="closeLightbox"
      @cancel.prevent="closeLightbox"
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
      <div class="lightbox-stage">
        <!-- No allow-same-origin: the page runs in an opaque origin, away from app storage. -->
        <iframe
          v-if="lightboxImage.kind === 'html'"
          class="lightbox-frame"
          sandbox="allow-scripts"
          referrerpolicy="no-referrer"
          :src="lightboxImage.url"
          :title="lightboxImage.name"
        ></iframe>
        <img v-else class="lightbox-img" :src="lightboxImage.url" :alt="lightboxImage.name" />
      </div>
    </dialog>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, watch, nextTick, onBeforeUnmount } from 'vue'
import { lightboxImage, closeLightbox } from '../composables/useLightbox'

const dialog = ref<HTMLDialogElement | null>(null)

watch(
  lightboxImage,
  async (img) => {
    if (!img) {
      dialog.value?.close()
      return
    }
    await nextTick()
    if (dialog.value && !dialog.value.open) dialog.value.showModal()
  },
  { immediate: true },
)
onBeforeUnmount(() => dialog.value?.close())
</script>

<style scoped>
.lightbox {
  margin: 0;
  border: 0;
  width: 100%;
  max-width: none;
  height: 100dvh;
  max-height: none;
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
.lightbox::backdrop {
  background: transparent;
}

.lightbox-bar {
  display: flex;
  flex-wrap: wrap;
  flex-shrink: 0;
  align-items: center;
  gap: 8px;
  width: 100%;
  max-width: 1200px;
}
.lightbox-name {
  flex: 1 1 12rem;
  min-width: 0;
  max-height: 25dvh;
  overflow-y: auto;
  overflow-wrap: anywhere;
  font: var(--text-small) / var(--leading-body) var(--font-ui);
  color: var(--color-text);
}
.lightbox-btn {
  padding: 6px 10px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 44px;
  min-height: 44px;
  text-decoration: none;
}
.lightbox-stage {
  flex: 1;
  min-height: 0;
  min-width: 0;
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
}
.lightbox-frame {
  width: 100%;
  max-width: 1200px;
  height: 100%;
  background: #fff;
  border: 2px solid var(--color-brass-dark);
}
.lightbox-img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  border: 2px solid var(--color-brass-dark);
}
</style>
