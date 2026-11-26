import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { API_BASE } from './api'

// Ensure all links generated from markdown open in a new tab safely.
// This hook runs once per module load and applies to every DOMPurify call in this app.
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A') {
    node.setAttribute('target', '_blank')
    node.setAttribute('rel', 'noopener noreferrer')
  }
})

// Accept public URLs and legacy artifact:// refs so stored messages still render.
const ARTIFACT_URL = String.raw`(?:artifact:\/\/[A-Za-z0-9-]+\/?|(?:https?:\/\/[^/\s()]+)?\/api\/artifacts\/[A-Za-z0-9-]+\/public)`
const ARTIFACT_RE = new RegExp(
  String.raw`!?\[([^\]\n]*)\]\((${ARTIFACT_URL})(?:\?type=(\w+))?\)|(^|\s)(${ARTIFACT_URL})(?:\?type=(\w+))?(?=$|[\s).,;!?])`,
  'g',
)

/** Permanent public artifact URL; mirrors backend `artifact_service.public_url()`. */
export function artifactUrl(id: string): string {
  return `${API_BASE}/api/artifacts/${id}/public`
}

// The type rides in the ref, so render without a per-artifact metadata fetch: images inline as a
// linked <img>, HTML as a link the `v-artifacts` directive previews, anything else as a plain link.
function inlineArtifacts(text: string): string {
  return text.replace(
    ARTIFACT_RE,
    (
      _m,
      label?: string,
      ref?: string,
      type?: string,
      pre?: string,
      bareRef?: string,
      bareType?: string,
    ) => {
      const reference = (ref ?? bareRef)!
      const url = reference.startsWith('artifact://')
        ? artifactUrl(reference.slice('artifact://'.length).replace(/\/$/, ''))
        : reference
      const kind = type ?? bareType ?? 'image'
      const name = (label ?? '').trim()
      if (kind === 'image') return `${pre ?? ''}[![${name || 'image'}](${url})](${url})`
      // The endpoint ignores the query; it just marks HTML links for the directive.
      const href = kind === 'html' ? `${url}?type=html` : url
      return `${pre ?? ''}[${kind === 'html' ? '🌐' : '📄'} ${name || kind}](${href})`
    },
  )
}

export function renderMarkdown(text: string): string {
  const html = marked.parse(inlineArtifacts(text), { async: false }) as string
  return DOMPurify.sanitize(html, { ADD_ATTR: ['target', 'rel'] })
}
