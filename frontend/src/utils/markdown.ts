import { marked } from 'marked'
import DOMPurify from 'dompurify'

// Ensure all links generated from markdown open in a new tab safely.
// This hook runs once per module load and applies to every DOMPurify call in this app.
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A') {
    node.setAttribute('target', '_blank')
    node.setAttribute('rel', 'noopener noreferrer')
  }
})

// Default DOMPurify URI allow-list plus our artifact:// scheme.
const ALLOWED_URI_REGEXP =
  /^(?:(?:https?|mailto|tel|artifact):|[^a-z]|[a-z+.-]+(?:[^a-z+.\-:]|$))/i

// Bare `artifact://<id>` (no link text) becomes a link so it renders like the others.
const BARE_ARTIFACT_RE = /(^|[\s(])(artifact:\/\/[A-Za-z0-9-]+)(?=$|[\s).,;!?])/g

function linkifyBareArtifacts(text: string): string {
  // Leave already-linked forms ](artifact://..) and <artifact://..> alone: the lookbehind
  // char class above excludes `]( ` prefixes because `(` is only matched after whitespace.
  return text.replace(BARE_ARTIFACT_RE, (_m, pre: string, url: string) => `${pre}[${url}](${url})`)
}

export function renderMarkdown(text: string): string {
  const html = marked.parse(linkifyBareArtifacts(text), { async: false }) as string
  return DOMPurify.sanitize(html, { ADD_ATTR: ['target', 'rel'], ALLOWED_URI_REGEXP })
}
