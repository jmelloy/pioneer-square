import { API_BASE } from './api'
import { artifactUrl } from './markdown'
import { useAuthStore } from '../stores/auth'

export interface UploadedArtifact {
  id: string
  url: string
  markdown: string
}

export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024

// extension -> content type; mirrors backend `artifact_service.ALLOWED_TYPES`.
const TYPES: Record<string, string> = {
  png: 'image/png',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  webp: 'image/webp',
  gif: 'image/gif',
  pdf: 'application/pdf',
  html: 'text/html',
  htm: 'text/html',
  txt: 'text/plain',
  md: 'text/markdown',
  csv: 'text/csv',
  json: 'application/json',
}
const KNOWN = new Set(Object.values(TYPES))

/** `<input accept>` value for the file picker. */
export const ACCEPT = Object.keys(TYPES)
  .map((e) => `.${e}`)
  .join(',')

/** Content type to upload as, or null when unsupported. Browsers often leave `.md` etc. untyped. */
export function contentType(file: File): string | null {
  if (KNOWN.has(file.type)) return file.type
  return TYPES[file.name.split('.').pop()?.toLowerCase() ?? ''] ?? null
}

/** Returns an error message, or null when the file is an acceptable artifact. */
export function validateFile(file: File): string | null {
  if (!contentType(file)) return `${file.name || 'File'} is not a supported file type`
  if (file.size === 0) return `${file.name || 'File'} is empty`
  if (file.size > MAX_UPLOAD_BYTES) {
    return `${file.name || 'File'} is larger than ${MAX_UPLOAD_BYTES / 1024 / 1024}MB`
  }
  return null
}

/** Extract supported files from a drop/paste DataTransfer. */
export function filesFromTransfer(dt: DataTransfer | null): File[] {
  if (!dt) return []
  const files: File[] = []
  if (dt.files && dt.files.length) {
    for (const f of Array.from(dt.files)) if (contentType(f)) files.push(f)
  } else if (dt.items) {
    for (const item of Array.from(dt.items)) {
      if (item.kind === 'file') {
        const f = item.getAsFile()
        if (f && contentType(f)) files.push(f)
      }
    }
  }
  return files
}

/** Multipart upload to `POST /guilds/{guild}/artifacts` with progress (0..1). */
export function uploadArtifact(
  guildId: string,
  file: File,
  onProgress: (fraction: number) => void,
  signal?: AbortSignal,
): Promise<UploadedArtifact> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `${API_BASE}/guilds/${encodeURIComponent(guildId)}/artifacts`)
    const headers = useAuthStore().authHeaders() as Record<string, string>
    for (const [k, v] of Object.entries(headers)) xhr.setRequestHeader(k, v)

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && e.total > 0) onProgress(e.loaded / e.total)
    }
    xhr.onload = () => {
      let body: Record<string, unknown> | null = null
      try {
        body = JSON.parse(xhr.responseText)
      } catch {
        /* non-JSON body */
      }
      if (xhr.status >= 200 && xhr.status < 300 && body && typeof body.id === 'string') {
        onProgress(1)
        const url = String(
          body.public_url ?? new URL(artifactUrl(body.id), window.location.origin).href,
        )
        const type = contentType(file)
        const kind = type?.startsWith('image/')
          ? 'image'
          : type === 'text/html'
            ? 'html'
            : type === 'application/pdf'
              ? 'pdf'
              : 'text'
        const href = kind === 'image' ? url : `${url}?type=${kind}`
        const name = file.name.replace(/[\[\]()\r\n]/g, '') || 'artifact'
        resolve({
          id: body.id,
          url,
          markdown: String(body.markdown ?? `[${name}](${href})`),
        })
      } else {
        reject(new Error(String(body?.detail ?? `Upload failed (HTTP ${xhr.status})`)))
      }
    }
    xhr.onerror = () => reject(new Error('Network error during upload'))
    xhr.onabort = () => reject(new DOMException('Upload cancelled', 'AbortError'))

    if (signal) {
      if (signal.aborted) return reject(new DOMException('Upload cancelled', 'AbortError'))
      signal.addEventListener('abort', () => xhr.abort(), { once: true })
    }
    const form = new FormData()
    // Re-type the blob so untyped files (e.g. `.md`) reach the backend with a usable type.
    const blob = file.slice(0, file.size, contentType(file) ?? file.type)
    form.append('file', blob, file.name || 'image.png')
    xhr.send(form)
  })
}
