<template>
  <div class="cost-summary">
    <div class="cost-header" @click="expanded = !expanded">
      <span class="cost-title">Cost (30d)</span>
      <span class="cost-total" v-if="totalCost !== null">${{ totalCost.toFixed(4) }}</span>
      <span class="collapse-icon">{{ expanded ? '▾' : '▸' }}</span>
    </div>
    <div v-if="expanded" class="cost-table-wrap">
      <div v-if="loading" class="cost-empty">Loading…</div>
      <div v-else-if="rows.length === 0" class="cost-empty">No completed tasks</div>
      <table v-else class="cost-table">
        <thead>
          <tr>
            <th>Tier</th>
            <th>Model</th>
            <th class="num">Tasks</th>
            <th class="num">Est. Cost</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="`${row.model_tier}-${row.model}`">
            <td>
              <span class="tier-badge" :class="tierClass(row.model_tier)">{{
                row.model_tier ?? 'legacy'
              }}</span>
            </td>
            <td class="model-cell">{{ row.model ?? '—' }}</td>
            <td class="num">{{ row.task_count }}</td>
            <td class="num">
              {{ row.estimated_cost_usd !== null ? '$' + row.estimated_cost_usd.toFixed(4) : '—' }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useGuildStore } from '../../stores/guild'
import { api } from '../../utils/api'

interface CostRow {
  model_tier: string | null
  model: string | null
  task_count: number
  estimated_cost_usd: number | null
}

const guildStore = useGuildStore()
const rows = ref<CostRow[]>([])
const loading = ref(false)
const expanded = ref(false)

const totalCost = computed(() => {
  const vals = rows.value.map((r) => r.estimated_cost_usd).filter((v) => v !== null) as number[]
  return vals.length > 0 ? vals.reduce((a, b) => a + b, 0) : null
})

function tierClass(tier: string | null) {
  if (tier === 'cheap') return 'tier-cheap'
  if (tier === 'powerful') return 'tier-powerful'
  if (tier === 'standard') return 'tier-standard'
  return 'tier-legacy'
}

async function fetchCost() {
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  loading.value = true
  try {
    rows.value = await api<CostRow[]>(`/guilds/${guildId}/cost-summary?days=30`)
  } catch {
    rows.value = []
  } finally {
    loading.value = false
  }
}

onMounted(fetchCost)
watch(() => guildStore.currentGuild?.id, fetchCost)
watch(expanded, (val) => {
  if (val && rows.value.length === 0 && !loading.value) fetchCost()
})
</script>

<style scoped>
.cost-summary {
  border-top: 1px solid var(--color-brass-dark);
  background: var(--color-bg-tertiary);
}

.cost-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  cursor: pointer;
  user-select: none;
}

.cost-header:hover {
  background: rgba(232, 170, 0, 0.06);
}

.cost-title {
  font-family: var(--font-pixel);
  font-size: 6px;
  letter-spacing: 1.5px;
  text-transform: uppercase;
  color: var(--color-text-dim);
  flex: 1;
}

.cost-total {
  font-size: 10px;
  color: var(--color-green);
  font-family: monospace;
}

.collapse-icon {
  font-size: 10px;
  color: var(--color-text-dim);
}

.cost-table-wrap {
  padding: 0 8px 8px;
}

.cost-empty {
  font-size: 10px;
  color: var(--color-text-dim);
  padding: 4px 4px 0;
}

.cost-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 9px;
}

.cost-table th {
  color: var(--color-text-dim);
  font-weight: normal;
  text-align: left;
  padding: 2px 4px;
  border-bottom: 1px solid var(--color-brass-dark);
}

.cost-table td {
  padding: 2px 4px;
  color: var(--color-text);
  vertical-align: middle;
}

.cost-table .num {
  text-align: right;
  font-family: monospace;
}

.model-cell {
  font-family: monospace;
  font-size: 8px;
  max-width: 100px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tier-badge {
  display: inline-block;
  padding: 1px 4px;
  border-radius: 2px;
  font-size: 8px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.tier-cheap {
  background: rgba(76, 175, 80, 0.2);
  color: var(--color-green);
}

.tier-standard {
  background: rgba(232, 170, 0, 0.15);
  color: var(--color-brass);
}

.tier-powerful {
  background: rgba(244, 67, 54, 0.15);
  color: var(--color-red);
}

.tier-legacy {
  background: rgba(128, 128, 128, 0.15);
  color: var(--color-text-dim);
}
</style>
