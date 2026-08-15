/**
 * Thread UI Components — Barrel export
 *
 * Foreman-managed thread components for the Pioneer Square frontend.
 * Issue #1169
 *
 * Components:
 *   - ThreadList: Filterable list of threads with create functionality
 *   - ThreadItem: Individual thread row with status indicator and metadata
 *   - ThreadView: Full thread detail/conversation view with lifecycle actions
 *
 * Composable:
 *   - useThreads: Reactive thread state management hook (wraps the threads store)
 */
export { default as ThreadList } from './ThreadList.vue'
export { default as ThreadItem } from './ThreadItem.vue'
export { default as ThreadView } from './ThreadView.vue'
export { useThreads } from './useThreads'
export type { UseThreadsOptions } from './useThreads'
