/**
 * Composable for Foreman-managed thread state and API interactions.
 * Wraps the threads Pinia store with additional reactive helpers for
 * component-local concerns (loading states, polling, auto-refresh).
 *
 * Issue #1169
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useGuildStore } from '../../stores/guild'
import { useThreadsStore } from '../../stores/threads'
import { useUiStore } from '../../stores/ui'
import type { ConversationThread, ThreadStatus } from '../../types'

export interface UseThreadsOptions {
  /** Auto-fetch threads on mount. Default true. */
  autoFetch?: boolean
  /** Auto-refresh interval in ms. 0 = disabled. Default 0 (no polling). */
  pollInterval?: number
  /** Initial status filter. Default undefined (all). */
  initialFilter?: ThreadStatus
}

export function useThreads(options: UseThreadsOptions = {}) {
  const { autoFetch = true, pollInterval = 0, initialFilter } = options

  const guildStore = useGuildStore()
  const threadsStore = useThreadsStore()
  const uiStore = useUiStore()

  const loading = ref(false)
  const error = ref<string | null>(null)
  const statusFilter = ref<ThreadStatus | undefined>(initialFilter)

  let pollTimer: ReturnType<typeof setInterval> | null = null

  const guildId = computed(() => guildStore.currentGuild?.id ?? null)

  const threads = computed(() => threadsStore.threads)

  const activeThreads = computed(() => threads.value.filter((t) => t.status === 'active'))
  const archivedThreads = computed(() => threads.value.filter((t) => t.status === 'archived'))
  const closedThreads = computed(() => threads.value.filter((t) => t.status === 'closed'))

  const selectedThread = computed<ConversationThread | undefined>(() => {
    const id = uiStore.selectedThreadId
    if (!id) return undefined
    return threads.value.find((t) => t.id === id)
  })

  async function fetchThreads() {
    const id = guildId.value
    if (!id) return
    loading.value = true
    error.value = null
    try {
      await threadsStore.fetchThreads(id, statusFilter.value)
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Failed to fetch threads'
    } finally {
      loading.value = false
    }
  }

  async function fetchThread(threadId: string) {
    const id = guildId.value
    if (!id) return null
    loading.value = true
    error.value = null
    try {
      return await threadsStore.fetchThread(id, threadId)
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Failed to fetch thread'
      return null
    } finally {
      loading.value = false
    }
  }

  async function createThread(name?: string) {
    const id = guildId.value
    if (!id) return null
    error.value = null
    try {
      const thread = await threadsStore.createThread(id, { name: name?.trim() || undefined })
      return thread
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Failed to create thread'
      return null
    }
  }

  async function archiveThread(threadId: string) {
    const id = guildId.value
    if (!id) return null
    error.value = null
    try {
      return await threadsStore.archiveThread(id, threadId)
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Failed to archive thread'
      return null
    }
  }

  async function closeThread(threadId: string) {
    const id = guildId.value
    if (!id) return null
    error.value = null
    try {
      return await threadsStore.closeThread(id, threadId)
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Failed to close thread'
      return null
    }
  }

  function selectThread(threadId: string | null) {
    uiStore.selectThread(threadId)
    if (threadId) uiStore.openThreadTab(threadId)
  }

  function setFilter(status: ThreadStatus | undefined) {
    statusFilter.value = status
    fetchThreads()
  }

  // Polling setup
  function startPolling() {
    stopPolling()
    if (pollInterval > 0) {
      pollTimer = setInterval(fetchThreads, pollInterval)
    }
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  }

  // Auto-fetch on guild change
  watch(guildId, (newId) => {
    if (newId && autoFetch) fetchThreads()
  })

  onMounted(() => {
    if (autoFetch && guildId.value) fetchThreads()
    startPolling()
  })

  onUnmounted(() => {
    stopPolling()
  })

  return {
    // State
    threads,
    activeThreads,
    archivedThreads,
    closedThreads,
    selectedThread,
    loading,
    error,
    statusFilter,

    // Actions
    fetchThreads,
    fetchThread,
    createThread,
    archiveThread,
    closeThread,
    selectThread,
    setFilter,
  }
}
