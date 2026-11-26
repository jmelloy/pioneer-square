import { describe, it, expect, vi } from 'vitest'

vi.mock('../api', () => ({ API_BASE: 'https://ps.example' }))
vi.mock('../../composables/useLightbox', () => ({ openLightbox: vi.fn() }))

import { renderMarkdown } from '../markdown'
import { enhanceArtifactImages } from '../artifacts'
import { openLightbox } from '../../composables/useLightbox'

const ID = '123e4567-e89b-12d3-a456-426614174000'
const URL = `https://ps.example/api/artifacts/${ID}/public`

describe.each([`artifact://${ID}`, URL])('artifact refs: %s', (ref) => {
  it('render as inline images with the public URL, incl. bare refs', () => {
    const html = renderMarkdown(`[shot.png](${ref})\n\nalso ${ref} here`)
    expect(html).not.toContain('artifact://')
    expect(html.match(new RegExp(`<img src="${URL}" alt="(shot.png|image)">`, 'g'))).toHaveLength(2)
    expect(html).toContain(`href="${URL}"`)
  })

  it('open the lightbox on click', () => {
    const el = document.createElement('div')
    el.innerHTML = renderMarkdown(`[shot.png](${ref})`)
    enhanceArtifactImages(el)
    el.querySelector('a')!.click()
    expect(openLightbox).toHaveBeenCalledWith({ url: URL, name: 'shot.png' })
  })

  it('render HTML artifacts as a sandboxed-preview link', () => {
    const el = document.createElement('div')
    el.innerHTML = renderMarkdown(`[report.html](${ref}?type=html)`)
    expect(el.querySelector('img')).toBeNull()
    enhanceArtifactImages(el)
    el.querySelector('a')!.click()
    expect(openLightbox).toHaveBeenCalledWith({
      url: `${URL}?type=html`,
      name: 'report.html',
      kind: 'html',
    })
  })

  it('render other types as plain links, incl. bare refs', () => {
    const html = renderMarkdown(`see ${ref}?type=pdf now`)
    expect(html).toContain(`href="${URL}"`)
    expect(html).toContain('📄 pdf')
    expect(html).not.toContain('<img')
  })
})
