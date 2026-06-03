<template>
  <div v-if="usage" class="usage-panel">
    <div class="usage-row usage-head">
      <span class="usage-label">CLAUDE USAGE</span>
      <span class="usage-meta">
        {{ usage.apiCalls }} call{{ usage.apiCalls === 1 ? '' : 's' }}
        <template v-if="usage.numTurns != null"> · {{ usage.numTurns }} turns</template>
        <template v-if="usage.model"> · {{ shortModel }}</template>
      </span>
    </div>

    <table class="usage-table">
      <thead>
        <tr>
          <th></th>
          <th>in</th>
          <th>out</th>
          <th>cache r</th>
          <th>cache w</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td class="usage-rowlabel" title="Sum of each assistant message's usage">Σ per-call</td>
          <td>{{ fmt(usage.summedInputTokens) }}</td>
          <td>{{ fmt(usage.summedOutputTokens) }}</td>
          <td>{{ fmt(usage.summedCacheReadTokens) }}</td>
          <td>{{ fmt(usage.summedCacheCreationTokens) }}</td>
        </tr>
        <tr v-if="hasReported">
          <td class="usage-rowlabel" title="The result event's self-reported totals">reported</td>
          <td :class="cellClass(usage.reportedInputTokens, usage.summedInputTokens)">
            {{ fmt(usage.reportedInputTokens) }}
          </td>
          <td :class="cellClass(usage.reportedOutputTokens, usage.summedOutputTokens)">
            {{ fmt(usage.reportedOutputTokens) }}
          </td>
          <td :class="cellClass(usage.reportedCacheReadTokens, usage.summedCacheReadTokens)">
            {{ fmt(usage.reportedCacheReadTokens) }}
          </td>
          <td
            :class="cellClass(usage.reportedCacheCreationTokens, usage.summedCacheCreationTokens)"
          >
            {{ fmt(usage.reportedCacheCreationTokens) }}
          </td>
        </tr>
      </tbody>
    </table>

    <div class="usage-row usage-foot">
      <span v-if="usage.costUsd != null" class="usage-cost">${{ usage.costUsd.toFixed(4) }}</span>
      <span v-if="mismatch" class="usage-warn" title="Per-call sum disagrees with reported totals">
        ⚠ sum ≠ reported
      </span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useUsageStore } from '../../stores/usage'

const props = defineProps<{ taskId: string }>()
const usageStore = useUsageStore()

const usage = computed(() => usageStore.usageForTask(props.taskId))

const shortModel = computed(() => (usage.value?.model || '').replace(/^claude-/, ''))

const hasReported = computed(() => usage.value?.reportedInputTokens != null)

function fmt(n: number | null | undefined): string {
  return (n ?? 0).toLocaleString()
}

// Highlight a reported cell when it differs from the per-call sum.
function cellClass(reported: number | null | undefined, summed: number): string {
  return reported != null && reported !== summed ? 'usage-diff' : ''
}

const mismatch = computed(() => {
  const u = usage.value
  if (!u || !hasReported.value) return false
  return (
    u.reportedInputTokens !== u.summedInputTokens ||
    u.reportedOutputTokens !== u.summedOutputTokens ||
    u.reportedCacheReadTokens !== u.summedCacheReadTokens ||
    u.reportedCacheCreationTokens !== u.summedCacheCreationTokens
  )
})
</script>

<style scoped>
.usage-panel {
  padding: 6px 10px 8px;
  border-top: 1px solid var(--color-brass-dark);
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--color-text-dim);
}

.usage-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.usage-label {
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 1px;
  color: var(--color-brass-light);
}

.usage-meta {
  font-size: 9px;
  color: var(--color-text-dim);
}

.usage-table {
  width: 100%;
  border-collapse: collapse;
  margin: 4px 0;
}

.usage-table th {
  font-weight: normal;
  text-align: right;
  color: var(--color-text-dim);
  opacity: 0.7;
  font-size: 9px;
  padding: 1px 4px;
}

.usage-table td {
  text-align: right;
  padding: 1px 4px;
  color: var(--color-text);
}

.usage-rowlabel {
  text-align: left !important;
  color: var(--color-text-dim) !important;
}

.usage-diff {
  color: var(--color-brass-light);
  font-weight: bold;
}

.usage-foot {
  margin-top: 2px;
}

.usage-cost {
  color: var(--color-teal);
}

.usage-warn {
  color: var(--color-brass-light);
  font-size: 9px;
}
</style>
