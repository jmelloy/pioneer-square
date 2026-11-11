import type { Directive } from 'vue'
import { api } from './api'
import { openLightbox } from '../composables/useLightbox'

// Messages reference artifacts as markdown links: `[shot.png](artifact://<uuid>)`.
// renderMarkdown() keeps those links; this directive resolves them to previews.

interface ArtifactMeta {
  id: string
  url: string
  contentType: string
}

const TTL_MS = 10 * 60 * 1000 // presigned URLs expire; refetch periodically
const cache = new Map<string, { at: number; promise: Promise<ArtifactMeta | null> }>()

export function parseArtifactId(href: string | null): string | null {
  const m = /^artifact:\/\/([A-Za-z0-9-]+)\/?$/.exec(href ?? '')
  return m ? m[1] : null
}

export function fetchArtifact(guildId: string, id: string): Promise<ArtifactMeta | null> {
  const key = `${guildId}/${id}`
  const hit = cache.get(key)
  if (hit && Date.now() - hit.at < TTL_MS) return hit.promise
  const promise = api<ArtifactMeta>(
    `/guilds/${encodeURIComponent(guildId)}/artifacts/${encodeURIComponent(id)}`,
  ).catch(() => null) // missing/expired/forbidden -> graceful fallback
  cache.set(key, { at: Date.now(), promise })
  return promise
}

function isSafeHttpUrl(url: string): boolean {
  return /^https?:\/\//i.test(url)
}

export async function hydrateArtifactLinks(root: HTMLElement, guildId: string): Promise<void> {
  const anchors = Array.from(root.querySelectorAll<HTMLAnchorElement>('a[href^="artifact://"]'))
  await Promise.all(
    anchors.map(async (a) => {
      const id = parseArtifactId(a.getAttribute('href'))
      if (!id) return
      const label = a.textContent && !a.textContent.startsWith('artifact://') ? a.textContent : id
      const meta = await fetchArtifact(guildId, id)
      if (!a.isConnected) return
      if (!meta || !isSafeHttpUrl(meta.url)) {
        const span = document.createElement('span')
        span.className = 'artifact-missing'
        span.textContent = `${label} (unavailable)`
        a.replaceWith(span)
        return
      }
      a.setAttribute('href', meta.url)
      if (meta.contentType?.startsWith('image/')) {
        const img = document.createElement('img')
        img.className = 'artifact-image'
        img.src = meta.url
        img.alt = label
        img.loading = 'lazy'
        img.style.maxWidth = '100%'
        a.textContent = ''
        a.appendChild(img)
        a.classList.add('artifact-thumb')
        a.addEventListener('click', (e) => {
          e.preventDefault()
          openLightbox({ url: meta.url, name: label })
        })
      } else {
        a.setAttribute('download', label)
      }
    }),
  )
}

/** `v-artifacts="guildId"` — place on an element rendered with v-html. */
export const vArtifacts: Directive<HTMLElement, string | undefined> = {
  mounted(el, binding) {
    if (binding.value) void hydrateArtifactLinks(el, binding.value)
  },
  updated(el, binding) {
    if (binding.value) void hydrateArtifactLinks(el, binding.value)
  },
}
