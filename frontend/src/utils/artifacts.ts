import type { Directive } from 'vue'
import { openLightbox } from '../composables/useLightbox'

// renderMarkdown() turns `artifact://<id>` refs into `<a><img src=".../api/artifacts/<id>/public"></a>`.
// This directive styles them and opens the lightbox instead of a new tab.
const SELECTOR = 'a > img[src*="/api/artifacts/"]'

export function enhanceArtifactImages(root: HTMLElement): void {
  for (const img of root.querySelectorAll<HTMLImageElement>(SELECTOR)) {
    const a = img.parentElement as HTMLAnchorElement
    if (a.classList.contains('artifact-thumb')) continue
    a.classList.add('artifact-thumb')
    img.classList.add('artifact-image')
    img.loading = 'lazy'
    img.style.maxWidth = '100%'
    a.addEventListener('click', (e) => {
      e.preventDefault()
      openLightbox({ url: img.src, name: img.alt })
    })
  }
}

/** `v-artifacts` — place on an element rendered with v-html. */
export const vArtifacts: Directive<HTMLElement, unknown> = {
  mounted: enhanceArtifactImages,
  updated: enhanceArtifactImages,
}
