import { ref } from 'vue'

export interface LightboxImage {
  url: string
  name: string
}

// Module-level singleton so any component can open the shared <ImageLightbox />.
export const lightboxImage = ref<LightboxImage | null>(null)

export function openLightbox(image: LightboxImage) {
  lightboxImage.value = image
}

export function closeLightbox() {
  lightboxImage.value = null
}

export function useLightbox() {
  return { lightboxImage, openLightbox, closeLightbox }
}
