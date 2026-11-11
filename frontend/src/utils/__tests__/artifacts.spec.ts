import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../api', () => ({ api: vi.fn() }))

import { api } from '../api'
import { renderMarkdown } from '../markdown'
import { hydrateArtifactLinks, parseArtifactId } from '../artifacts'

const ID = '123e4567-e89b-12d3-a456-426614174000'

describe('artifact:// links', () => {
  beforeEach(() => vi.mocked(api).mockReset())

  it('keeps artifact links through markdown sanitising, incl. bare URLs', () => {
    const html = renderMarkdown(`[shot.png](artifact://${ID})\n\nalso artifact://${ID}-b here`)
    expect(html).toContain(`href="artifact://${ID}"`)
    expect(html).toContain(`href="artifact://${ID}-b"`)
    expect(parseArtifactId(`artifact://${ID}`)).toBe(ID)
    expect(parseArtifactId('https://x/y')).toBeNull()
  })

  it('renders images inline and degrades gracefully when missing', async () => {
    vi.mocked(api).mockResolvedValueOnce({
      id: ID,
      url: 'https://s3.example/x.png',
      contentType: 'image/png',
    })
    const ok = document.createElement('div')
    ok.innerHTML = renderMarkdown(`[shot.png](artifact://${ID})`)
    document.body.append(ok)
    await hydrateArtifactLinks(ok, 'g-ok')
    expect(ok.querySelector('img')?.getAttribute('src')).toBe('https://s3.example/x.png')

    vi.mocked(api).mockRejectedValueOnce(new Error('404'))
    const bad = document.createElement('div')
    bad.innerHTML = renderMarkdown(`[gone.png](artifact://${ID}-gone)`)
    document.body.append(bad)
    await hydrateArtifactLinks(bad, 'g-bad')
    expect(bad.querySelector('.artifact-missing')?.textContent).toContain('gone.png')
  })
})
