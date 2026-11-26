import type { Directive } from 'vue'
import { openLightbox } from '../composables/useLightbox'

// renderMarkdown() turns public artifact URLs (and legacy refs) into linked images.
// (HTML artifacts: `<a href=".../public?type=html">`). This directive styles them and opens the
// lightbox instead of a new tab.
const SELECTOR = 'a > img[src*="/api/artifacts/"]'
const HTML_SELECTOR = 'a[href*="/api/artifacts/"][href$="?type=html"]'

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
  for (const a of root.querySelectorAll<HTMLAnchorElement>(HTML_SELECTOR)) {
    if (a.classList.contains('artifact-html')) continue
    a.classList.add('artifact-html')
    a.addEventListener('click', (e) => {
      e.preventDefault()
      openLightbox({ url: a.href, name: a.textContent?.replace(/^🌐\s*/, '') ?? '', kind: 'html' })
    })
  }
}

/** `v-artifacts` — place on an element rendered with v-html. */
export const vArtifacts: Directive<HTMLElement, unknown> = {
  mounted: enhanceArtifactImages,
  updated: enhanceArtifactImages,
}
