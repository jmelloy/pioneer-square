import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import AttachmentTray from '../AttachmentTray.vue'
import type { Attachment } from '../../composables/useAttachments'

const attachment: Attachment = {
  key: 7,
  file: new File(['image'], 'screenshot.png', { type: 'image/png' }),
  previewUrl: '/preview.png',
  status: 'uploading',
  progress: 0.4,
  error: null,
  markdown: null,
  controller: null,
}

describe('AttachmentTray', () => {
  it('names upload progress and exposes its range', () => {
    const wrapper = mount(AttachmentTray, { props: { attachments: [attachment] } })
    const progress = wrapper.find('[role="progressbar"]')
    expect(progress.attributes('aria-label')).toBe('Uploading screenshot.png')
    expect(progress.attributes('aria-valuenow')).toBe('40')
    expect(progress.attributes('aria-valuemin')).toBe('0')
    expect(progress.attributes('aria-valuemax')).toBe('100')
  })

  it('announces failures and preserves retry/remove events', async () => {
    const wrapper = mount(AttachmentTray, {
      props: { attachments: [{ ...attachment, status: 'error', error: 'Upload failed' }] },
    })
    expect(wrapper.find('[role="status"]').text()).toBe('Upload failed')
    await wrapper.find('.tray-retry').trigger('click')
    await wrapper.find('.tray-remove').trigger('click')
    expect(wrapper.emitted('retry')).toEqual([[7]])
    expect(wrapper.emitted('remove')).toEqual([[7]])
  })

  it('announces validation notices without an upload', () => {
    const wrapper = mount(AttachmentTray, {
      props: { attachments: [], notice: 'File is not an image' },
    })
    expect(wrapper.find('[role="status"]').text()).toBe('File is not an image')
  })
})
