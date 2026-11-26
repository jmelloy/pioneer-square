import { afterEach, describe, it, expect, vi } from 'vitest'
import {
  validateFile,
  filesFromTransfer,
  contentType,
  MAX_UPLOAD_BYTES,
  uploadArtifact,
} from '../artifactUpload'

vi.mock('../../stores/auth', () => ({ useAuthStore: () => ({ authHeaders: () => ({}) }) }))
afterEach(() => vi.restoreAllMocks())

const file = (name: string, type: string, size: number) => {
  const f = new File(['x'], name, { type })
  Object.defineProperty(f, 'size', { value: size })
  return f
}

describe('validateFile', () => {
  it('accepts a normal image', () => {
    expect(validateFile(file('a.png', 'image/png', 100))).toBeNull()
  })
  it('accepts documents, typing untyped ones by extension', () => {
    expect(validateFile(file('a.txt', 'text/plain', 100))).toBeNull()
    expect(contentType(file('r.HTML', '', 1))).toBe('text/html')
    expect(contentType(file('n.md', '', 1))).toBe('text/markdown')
  })
  it('rejects unsupported files', () => {
    expect(validateFile(file('a.exe', 'application/x-msdownload', 100))).toMatch(/not a supported/)
    expect(validateFile(file('a.svg', 'image/svg+xml', 100))).toMatch(/not a supported/)
  })
  it('rejects empty files', () => {
    expect(validateFile(file('a.png', 'image/png', 0))).toMatch(/empty/)
  })
  it('rejects files over 10MB but allows exactly 10MB', () => {
    expect(validateFile(file('a.png', 'image/png', MAX_UPLOAD_BYTES + 1))).toMatch(/larger/)
    expect(validateFile(file('a.png', 'image/png', MAX_UPLOAD_BYTES))).toBeNull()
  })
})

describe('uploadArtifact', () => {
  it.each([
    ['shot.png', 'image/png', ''],
    ['report.html', 'text/html', '?type=html'],
    ['report.pdf', 'application/pdf', '?type=pdf'],
    ['report.md', '', '?type=text'],
  ])('falls back to public markdown for %s', async (name, type, hint) => {
    const url = 'https://public.example/api/artifacts/id-1/public'
    vi.spyOn(XMLHttpRequest.prototype, 'send').mockImplementation(function () {
      Object.defineProperties(this, {
        status: { value: 200 },
        responseText: { value: JSON.stringify({ id: 'id-1', public_url: url }) },
      })
      this.dispatchEvent(new Event('load'))
    })
    const result = await uploadArtifact('guild', file(name, type, 1), vi.fn())
    expect(result.url).toBe(url)
    expect(result.markdown).toBe(`[${name}](${url}${hint})`)
  })

  it('uses the browser origin when no public URL is returned', async () => {
    vi.spyOn(XMLHttpRequest.prototype, 'send').mockImplementation(function () {
      Object.defineProperties(this, {
        status: { value: 200 },
        responseText: { value: JSON.stringify({ id: 'id-1' }) },
      })
      this.dispatchEvent(new Event('load'))
    })
    const result = await uploadArtifact('guild', file('shot.png', 'image/png', 1), vi.fn())
    expect(result.markdown).toBe(`[shot.png](${window.location.origin}/api/artifacts/id-1/public)`)
  })
})

describe('filesFromTransfer', () => {
  it('handles null', () => expect(filesFromTransfer(null)).toEqual([]))
  it('keeps only supported files', () => {
    const img = file('a.png', 'image/png', 1)
    const html = file('r.html', 'text/html', 1)
    const dt = {
      files: [img, html, file('a.zip', 'application/zip', 1)],
    } as unknown as DataTransfer
    expect(filesFromTransfer(dt)).toEqual([img, html])
  })
})
