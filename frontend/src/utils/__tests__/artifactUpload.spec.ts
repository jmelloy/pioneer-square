import { describe, it, expect } from 'vitest'
import { validateImage, imagesFromTransfer, MAX_UPLOAD_BYTES } from '../artifactUpload'

const file = (name: string, type: string, size: number) => {
  const f = new File(['x'], name, { type })
  Object.defineProperty(f, 'size', { value: size })
  return f
}

describe('validateImage', () => {
  it('accepts a normal image', () => {
    expect(validateImage(file('a.png', 'image/png', 100))).toBeNull()
  })
  it('rejects non-images', () => {
    expect(validateImage(file('a.txt', 'text/plain', 100))).toMatch(/not an image/)
  })
  it('rejects empty files', () => {
    expect(validateImage(file('a.png', 'image/png', 0))).toMatch(/empty/)
  })
  it('rejects files over 10MB but allows exactly 10MB', () => {
    expect(validateImage(file('a.png', 'image/png', MAX_UPLOAD_BYTES + 1))).toMatch(/larger/)
    expect(validateImage(file('a.png', 'image/png', MAX_UPLOAD_BYTES))).toBeNull()
  })
})

describe('imagesFromTransfer', () => {
  it('handles null', () => expect(imagesFromTransfer(null)).toEqual([]))
  it('keeps only image files', () => {
    const img = file('a.png', 'image/png', 1)
    const dt = { files: [img, file('a.txt', 'text/plain', 1)] } as unknown as DataTransfer
    expect(imagesFromTransfer(dt)).toEqual([img])
  })
})
