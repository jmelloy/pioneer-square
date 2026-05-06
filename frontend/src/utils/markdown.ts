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

export function renderMarkdown(text: string): string {
  const html = marked.parse(text, { async: false }) as string
  return DOMPurify.sanitize(html, { ADD_ATTR: ['target', 'rel'] })
}
