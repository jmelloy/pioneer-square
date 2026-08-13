import { beforeEach, describe, expect, it } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { taskTabId, useUiStore } from '../ui'

describe('useUiStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  describe('selectWorker / closeWorker', () => {
    it('selectWorker sets selectedWorkerId, opens the tab (no duplicates), and activates it', () => {
      const store = useUiStore()
      store.selectWorker('w-1')
      store.selectWorker('w-1')

      expect(store.selectedWorkerId).toBe('w-1')
      expect(store.openedWorkerIds).toEqual(['w-1'])
      expect(store.activeTab).toBe('worker-w-1')
    })

    it('closeWorker removes the id, clears selection, and falls back to factory if active', () => {
      const store = useUiStore()
      store.selectWorker('w-1')
      store.closeWorker('w-1')

      expect(store.openedWorkerIds).toEqual([])
      expect(store.selectedWorkerId).toBeNull()
      expect(store.activeTab).toBe('factory')
    })

    it('closeWorker leaves activeTab alone when a different tab is active', () => {
      const store = useUiStore()
      store.selectWorker('w-1')
      store.selectWorker('w-2')
      store.closeWorker('w-1')

      expect(store.openedWorkerIds).toEqual(['w-2'])
      expect(store.activeTab).toBe('worker-w-2')
    })
  })

  describe('selectAgent / closeAgent', () => {
    it('selectAgent sets selectedAgentId, opens the tab (no duplicates), and activates it', () => {
      const store = useUiStore()
      store.selectAgent('a-1')
      store.selectAgent('a-1')

      expect(store.selectedAgentId).toBe('a-1')
      expect(store.openedAgentIds).toEqual(['a-1'])
      expect(store.activeTab).toBe('agent-a-1')
    })

    it('closeAgent removes the id, clears selection, and falls back to factory if active', () => {
      const store = useUiStore()
      store.selectAgent('a-1')
      store.closeAgent('a-1')

      expect(store.openedAgentIds).toEqual([])
      expect(store.selectedAgentId).toBeNull()
      expect(store.activeTab).toBe('factory')
    })
  })

  describe('selectTask / closeTask / openTaskTab', () => {
    it('selectTask tracks selectedTaskId and openedTaskIds (no duplicates)', () => {
      const store = useUiStore()
      store.selectTask('t-1')
      store.selectTask('t-1')
      store.selectTask('t-2')

      expect(store.selectedTaskId).toBe('t-2')
      expect(store.openedTaskIds).toEqual(['t-1', 't-2'])
    })

    it('closeTask removes id and clears selectedTaskId if matching', () => {
      const store = useUiStore()
      store.selectTask('t-1')
      store.selectTask('t-2')
      store.closeTask('t-2')

      expect(store.openedTaskIds).toEqual(['t-1'])
      expect(store.selectedTaskId).toBeNull()
    })

    it('closeTask resets activeTab to factory when the closed task tab was active', () => {
      const store = useUiStore()
      store.selectTask('t-1')
      store.openTaskTab('t-1')
      expect(store.activeTab).toBe(taskTabId('t-1'))

      store.closeTask('t-1')
      expect(store.activeTab).toBe('factory')
    })
  })

  describe('resetWorkerAgentSelection / resetTaskSelection', () => {
    it('resetWorkerAgentSelection clears worker/agent selection and activeTab, leaves task state', () => {
      const store = useUiStore()
      store.selectWorker('w-1')
      store.selectAgent('a-1')
      store.selectTask('t-1')

      store.resetWorkerAgentSelection()

      expect(store.selectedWorkerId).toBeNull()
      expect(store.openedWorkerIds).toEqual([])
      expect(store.selectedAgentId).toBeNull()
      expect(store.openedAgentIds).toEqual([])
      expect(store.activeTab).toBe('factory')
      expect(store.selectedTaskId).toBe('t-1')
    })

    it('resetTaskSelection clears task selection, leaves worker/agent state', () => {
      const store = useUiStore()
      store.selectWorker('w-1')
      store.selectTask('t-1')

      store.resetTaskSelection()

      expect(store.selectedTaskId).toBeNull()
      expect(store.openedTaskIds).toEqual([])
      expect(store.selectedWorkerId).toBe('w-1')
    })
  })
})
