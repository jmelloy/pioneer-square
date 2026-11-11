import { ref, computed, onBeforeUnmount, type Ref } from 'vue'
import { imagesFromTransfer, uploadArtifact, validateImage } from '../utils/artifactUpload'

export interface Attachment {
  key: number
  file: File
  previewUrl: string
  status: 'uploading' | 'done' | 'error'
  progress: number
  error: string | null
  markdown: string | null
  controller: AbortController | null
}

let nextKey = 1

export function useAttachments(guildId: () => string | undefined) {
  const attachments = ref<Attachment[]>([])
  const dragging = ref(false)
  const notice = ref<string | null>(null)

  const uploading = computed(() => attachments.value.some((a) => a.status === 'uploading'))
  const hasErrors = computed(() => attachments.value.some((a) => a.status === 'error'))
  const ready = computed(() => attachments.value.filter((a) => a.status === 'done'))

  function find(key: number) {
    return attachments.value.find((a) => a.key === key)
  }

  async function start(key: number) {
    const att = find(key)
    const gid = guildId()
    if (!att) return
    if (!gid) {
      att.status = 'error'
      att.error = 'No guild selected'
      return
    }
    const controller = new AbortController()
    att.controller = controller
    att.status = 'uploading'
    att.progress = 0
    att.error = null
    try {
      const res = await uploadArtifact(
        gid,
        att.file,
        (f) => {
          const a = find(key)
          if (a) a.progress = f
        },
        controller.signal,
      )
      const a = find(key)
      if (a) {
        a.status = 'done'
        a.markdown = res.markdown
      }
    } catch (e) {
      const a = find(key)
      if (a && !controller.signal.aborted) {
        a.status = 'error'
        a.error = e instanceof Error ? e.message : 'Upload failed'
      }
    } finally {
      const a = find(key)
      if (a) a.controller = null
    }
  }

  function addFiles(files: File[]) {
    notice.value = null
    for (const file of files) {
      const err = validateImage(file)
      if (err) {
        notice.value = err
        continue
      }
      const key = nextKey++
      attachments.value.push({
        key,
        file,
        previewUrl: URL.createObjectURL(file),
        status: 'uploading',
        progress: 0,
        error: null,
        markdown: null,
        controller: null,
      })
      void start(key)
    }
  }

  function release(a: Attachment) {
    a.controller?.abort()
    URL.revokeObjectURL(a.previewUrl)
  }

  function remove(key: number) {
    const a = find(key)
    if (!a) return
    release(a)
    attachments.value = attachments.value.filter((x) => x.key !== key)
  }

  function retry(key: number) {
    void start(key)
  }

  function clear() {
    attachments.value.forEach(release)
    attachments.value = []
    notice.value = null
  }

  function onPaste(e: ClipboardEvent) {
    const files = imagesFromTransfer(e.clipboardData)
    if (!files.length) return
    e.preventDefault()
    addFiles(files)
  }

  function onDragOver(e: DragEvent) {
    if (e.dataTransfer?.types?.includes('Files')) {
      e.preventDefault()
      dragging.value = true
    }
  }
  function onDragLeave(e: DragEvent) {
    // Ignore leave events fired when moving between child elements.
    if (e.currentTarget instanceof Node && e.relatedTarget instanceof Node) {
      if (e.currentTarget.contains(e.relatedTarget)) return
    }
    dragging.value = false
  }
  function onDrop(e: DragEvent) {
    dragging.value = false
    const files = imagesFromTransfer(e.dataTransfer)
    if (!files.length) return
    e.preventDefault()
    addFiles(files)
  }

  /** Text plus markdown references for every finished upload. */
  function compose(text: string): string {
    const refs = ready.value.map((a) => a.markdown).filter(Boolean) as string[]
    return [text.trim(), ...refs].filter(Boolean).join('\n\n')
  }

  onBeforeUnmount(clear)

  return {
    attachments: attachments as Ref<Attachment[]>,
    dragging,
    notice,
    uploading,
    hasErrors,
    ready,
    addFiles,
    remove,
    retry,
    clear,
    compose,
    onPaste,
    onDragOver,
    onDragLeave,
    onDrop,
  }
}
