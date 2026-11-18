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

// `[text](artifact://<id>)`, `![text](artifact://<id>)`, or a bare `artifact://<id>`.
const ARTIFACT_RE =
  /!?\[([^\]\n]*)\]\(artifact:\/\/([A-Za-z0-9-]+)\/?\)|(^|\s)artifact:\/\/([A-Za-z0-9-]+)\/?(?=$|[\s).,;!?])/g

/** Permanent public image URL; mirrors backend `artifact_service.public_url()`. */
export function artifactUrl(id: string): string {
  return `${API_BASE}/api/artifacts/${id}/public`
}

// Artifacts are always images (backend only accepts png/jpeg/webp/gif), so render them
// inline as a linked <img> — no per-artifact metadata fetch needed.
function inlineArtifacts(text: string): string {
  return text.replace(
    ARTIFACT_RE,
    (_m, label?: string, id?: string, pre?: string, bareId?: string) => {
      const url = artifactUrl((id ?? bareId)!)
      const alt = (label ?? '').trim() || 'image'
      return `${pre ?? ''}[![${alt}](${url})](${url})`
    },
  )
}

export function renderMarkdown(text: string): string {
  const html = marked.parse(inlineArtifacts(text), { async: false }) as string
  return DOMPurify.sanitize(html, { ADD_ATTR: ['target', 'rel'] })
}
