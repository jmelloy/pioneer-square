import { API_BASE } from './api'
import { useAuthStore } from '../stores/auth'

export interface UploadedArtifact {
  id: string
  url: string
  markdown: string
}

export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024

/** Returns an error message, or null when the file is an acceptable image. */
export function validateImage(file: File): string | null {
  if (!file.type.startsWith('image/')) return `${file.name || 'File'} is not an image`
  if (file.size === 0) return `${file.name || 'File'} is empty`
  if (file.size > MAX_UPLOAD_BYTES) {
    return `${file.name || 'File'} is larger than ${MAX_UPLOAD_BYTES / 1024 / 1024}MB`
  }
  return null
}

/** Extract image files from a drop/paste DataTransfer. */
export function imagesFromTransfer(dt: DataTransfer | null): File[] {
  if (!dt) return []
  const files: File[] = []
  if (dt.files && dt.files.length) {
    for (const f of Array.from(dt.files)) if (f.type.startsWith('image/')) files.push(f)
  } else if (dt.items) {
    for (const item of Array.from(dt.items)) {
      if (item.kind === 'file' && item.type.startsWith('image/')) {
        const f = item.getAsFile()
        if (f) files.push(f)
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
        resolve({
          id: body.id,
          url: String(body.url ?? ''),
          markdown: String(body.markdown ?? `[${file.name}](artifact://${body.id})`),
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
    form.append('file', file, file.name || 'image.png')
    xhr.send(form)
  })
}
