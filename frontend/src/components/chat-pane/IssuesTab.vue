<template>
  <div class="issues-toolbar">
    <button
      class="pixel-btn refresh-btn"
      @click="() => refreshIssues()"
      :disabled="ghStore.loading"
    >
      {{ ghStore.loading ? '...' : '↻' }}
    </button>
    <span class="issues-count">{{ ghStore.issues.length }} open</span>
    <span class="issues-repos">{{
      guildStore.currentGuild?.primary_repo ??
      ghStore.selectedRepos.length + ' repo' + (ghStore.selectedRepos.length !== 1 ? 's' : '')
    }}</span>
  </div>

  <div class="issues-list" ref="issuesEl">
    <div v-if="ghStore.issues.length === 0 && !ghStore.loading" class="chat-empty">
      No open issues found.
    </div>
    <div v-if="ghStore.loading" class="chat-empty">Loading issues...</div>

    <div
      v-for="issue in ghStore.issues"
      :key="issue.id"
      class="issue-row"
      @click="$emit('select-issue', issue)"
      :title="'Click to assign this issue'"
    >
      <div class="issue-top">
        <span class="issue-repo">{{ issue.repo }}</span>
        <span class="issue-number">#{{ issue.number }}</span>
      </div>
      <div class="issue-title">{{ issue.title }}</div>
      <div class="issue-meta">
        <span
          v-for="label in issue.labels.slice(0, 3)"
          :key="label.id"
          class="issue-label"
          :style="{ borderColor: '#' + label.color, color: '#' + label.color }"
          >{{ label.name }}</span
        >
        <span class="issue-age">{{ formatAge(issue.created_at) }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted } from 'vue'
import { useGuildStore } from '../../stores/guild'
import { useGitHubStore } from '../../stores/github'
import { formatAge } from '../../utils/format'
import type { GitHubIssue } from '../../types'

defineEmits<{ (e: 'select-issue', issue: GitHubIssue): void }>()

const guildStore = useGuildStore()
const ghStore = useGitHubStore()

const ISSUE_REFRESH_MS = 3 * 60 * 1000
let issueRefreshInterval: ReturnType<typeof setInterval> | null = null

function primaryRepoList(): string[] | undefined {
  const repo = guildStore.currentGuild?.primary_repo
  return repo ? [repo] : undefined
}

async function refreshIssues(silent = false) {
  await ghStore.fetchIssues(primaryRepoList(), silent)
}

defineExpose({ refreshIssues })

onMounted(() => {
  if (ghStore.issues.length === 0 && !ghStore.loading) {
    refreshIssues()
  }
  if (ghStore.isConfigured) {
    issueRefreshInterval = setInterval(() => refreshIssues(true), ISSUE_REFRESH_MS)
  }
})

onUnmounted(() => {
  if (issueRefreshInterval !== null) {
    clearInterval(issueRefreshInterval)
    issueRefreshInterval = null
  }
})
</script>

<style scoped>
.issues-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--color-brass-dark);
  flex-shrink: 0;
}

.refresh-btn {
  font-size: 10px;
  padding: 3px 7px;
}

.refresh-btn:disabled {
  opacity: 0.4;
  pointer-events: none;
}

.issues-count {
  font-size: 11px;
  color: var(--color-text-dim);
}

.issues-repos {
  margin-left: auto;
  font-size: 10px;
  color: var(--color-text-dim);
}

.issues-list {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  min-height: 180px;
  max-height: 380px;
}

.chat-empty {
  color: var(--color-text-dim);
  font-size: 11px;
  text-align: center;
  margin-top: 20px;
  font-style: italic;
}

.issue-row {
  padding: 9px 12px;
  border-bottom: 1px solid var(--color-bg-tertiary);
  cursor: pointer;
  transition: background 0.1s;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.issue-row:hover {
  background: rgba(0, 187, 170, 0.07);
}

.issue-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.issue-repo {
  font-size: 9px;
  color: var(--color-text-dim);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 220px;
}

.issue-number {
  font-family: var(--font-pixel);
  font-size: 7px;
  color: var(--color-brass-dark);
}

.issue-title {
  font-size: 12px;
  color: var(--color-text);
  line-height: 1.35;
}

.issue-meta {
  display: flex;
  align-items: center;
  gap: 5px;
  flex-wrap: wrap;
}

.issue-label {
  font-size: 9px;
  border: 1px solid;
  padding: 1px 5px;
  border-radius: 2px;
}

.issue-age {
  margin-left: auto;
  font-size: 9px;
  color: var(--color-text-dim);
}

@media (max-width: 1024px) {
  .issues-list {
    min-height: 0;
    max-height: none;
  }
}
</style>
