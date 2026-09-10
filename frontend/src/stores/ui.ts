import { defineStore } from 'pinia'
import { ref } from 'vue'

export function taskTabId(id: string): string {
  return 'task-' + id
}

export function conversationTabId(id: number): string {
  return 'conversation-' + id
}

// Single source of truth for "what's open/selected" across the main-content
// tab bar and sidebar. Previously this was split across agents.ts (worker/agent
// tabs) and tasks.ts (task tabs), which forced components to join by id across
// two stores and let the two copies of tab-close logic drift apart. Issue tabs
// are tracked separately in the github store, which owns issue-specific state.
export const useUiStore = defineStore('ui', () => {
  const activeTab = ref<string>('factory')

  const selectedWorkerId = ref<string | null>(null)
  const openedWorkerIds = ref<string[]>([])
  const selectedAgentId = ref<string | null>(null)
  const openedAgentIds = ref<string[]>([])
  const selectedTaskId = ref<string | null>(null)
  const openedTaskIds = ref<string[]>([])
  const selectedConversationId = ref<number | null>(null)
  const openedConversationIds = ref<number[]>([])

  function selectWorker(workerId: string | null) {
    selectedWorkerId.value = workerId
    if (workerId) {
      if (!openedWorkerIds.value.includes(workerId)) openedWorkerIds.value.push(workerId)
      activeTab.value = 'worker-' + workerId
    }
  }

  function closeWorker(workerId: string) {
    const idx = openedWorkerIds.value.indexOf(workerId)
    if (idx !== -1) openedWorkerIds.value.splice(idx, 1)
    if (selectedWorkerId.value === workerId) selectedWorkerId.value = null
    if (activeTab.value === 'worker-' + workerId) activeTab.value = 'factory'
  }

  function selectAgent(agentId: string | null) {
    selectedAgentId.value = agentId
    if (agentId) {
      if (!openedAgentIds.value.includes(agentId)) openedAgentIds.value.push(agentId)
      activeTab.value = 'agent-' + agentId
    }
  }

  function closeAgent(agentId: string) {
    const idx = openedAgentIds.value.indexOf(agentId)
    if (idx !== -1) openedAgentIds.value.splice(idx, 1)
    if (selectedAgentId.value === agentId) selectedAgentId.value = null
    if (activeTab.value === 'agent-' + agentId) activeTab.value = 'factory'
  }

  function selectTask(taskId: string | null) {
    selectedTaskId.value = taskId
    if (taskId && !openedTaskIds.value.includes(taskId)) {
      openedTaskIds.value.push(taskId)
    }
  }

  function closeTask(taskId: string) {
    const idx = openedTaskIds.value.indexOf(taskId)
    if (idx !== -1) openedTaskIds.value.splice(idx, 1)
    if (selectedTaskId.value === taskId) selectedTaskId.value = null
    if (activeTab.value === taskTabId(taskId)) activeTab.value = 'factory'
  }

  function openTaskTab(taskId: string) {
    activeTab.value = taskTabId(taskId)
  }

  function selectConversation(conversationId: number | null) {
    selectedConversationId.value = conversationId
    if (conversationId != null && !openedConversationIds.value.includes(conversationId)) {
      openedConversationIds.value.push(conversationId)
    }
  }

  function closeConversation(conversationId: number) {
    const idx = openedConversationIds.value.indexOf(conversationId)
    if (idx !== -1) openedConversationIds.value.splice(idx, 1)
    if (selectedConversationId.value === conversationId) selectedConversationId.value = null
    if (activeTab.value === conversationTabId(conversationId)) activeTab.value = 'factory'
  }

  function openConversationTab(conversationId: number) {
    activeTab.value = conversationTabId(conversationId)
  }

  // Resets worker/agent selection and the main tab, e.g. on guild switch.
  function resetWorkerAgentSelection() {
    selectedWorkerId.value = null
    openedWorkerIds.value = []
    selectedAgentId.value = null
    openedAgentIds.value = []
    activeTab.value = 'factory'
  }

  // Resets task selection, e.g. on guild switch or unmount.
  function resetTaskSelection() {
    selectedTaskId.value = null
    openedTaskIds.value = []
  }

  // Resets conversation selection, e.g. on guild switch or unmount.
  function resetConversationSelection() {
    selectedConversationId.value = null
    openedConversationIds.value = []
  }

  return {
    activeTab,
    selectedWorkerId,
    openedWorkerIds,
    selectedAgentId,
    openedAgentIds,
    selectedTaskId,
    openedTaskIds,
    selectedConversationId,
    openedConversationIds,
    selectWorker,
    closeWorker,
    selectAgent,
    closeAgent,
    selectTask,
    closeTask,
    openTaskTab,
    selectConversation,
    closeConversation,
    openConversationTab,
    resetWorkerAgentSelection,
    resetTaskSelection,
    resetConversationSelection,
  }
})
