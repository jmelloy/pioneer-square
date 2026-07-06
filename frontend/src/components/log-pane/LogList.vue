<template>
  <div class="pane-body" ref="bodyEl">
    <div v-if="logs.length === 0" class="logs-empty">
      <span class="cursor-blink">_</span> Waiting for output…
    </div>
    <template v-for="item in groupedLogs" :key="item.type === 'turn' ? `turn-${item.turnNumber}` : `line-${item.index}`">
      <LogLine
        v-if="item.type === 'line'"
        :log="item.log"
        :expanded="expandedLineIdx === item.index"
        @toggle="toggleLineDetail(item.index)"
      />
      <div v-else class="turn-group">
        <div
          class="turn-summary"
          @click="toggleTurn(item.turnNumber)"
        >
          <span class="turn-summary-icon">{{ expandedTurns.has(item.turnNumber) ? '▾' : '▸' }}</span>
          <span class="turn-summary-text">
            Turn {{ item.turnNumber }} — {{ formatTurnCounts(item.counts) }}
            ({{ item.toolCallCount }} tool call{{ item.toolCallCount === 1 ? '' : 's' }})
          </span>
        </div>
        <div v-if="expandedTurns.has(item.turnNumber)" class="turn-body">
          <LogLine
            v-for="entry in item.entries"
            :key="entry.index"
            :log="entry.log"
            :expanded="expandedTurnLineIdx === entry.index"
            @toggle="toggleTurnLineDetail(entry.index)"
          />
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick } from 'vue'
import LogLine from './LogLine.vue'
import type { LogEntry } from '../../types'

const props = defineProps<{ logs: LogEntry[] }>()

const bodyEl = ref<HTMLElement | null>(null)
// Separate expansion state for top-level lines vs. lines nested inside a turn
// body: both index into the same props.logs array, so sharing one ref would
// let expanding a line in one context incorrectly expand/collapse the entry
// at the same index in the other context.
const expandedLineIdx = ref<number | null>(null)
const expandedTurnLineIdx = ref<number | null>(null)
const expandedTurns = ref<Set<number>>(new Set())

interface LineItem {
  type: 'line'
  log: LogEntry
  index: number
}

interface TurnItem {
  type: 'turn'
  turnNumber: number
  entries: { log: LogEntry; index: number }[]
  counts: Record<string, number>
  toolCallCount: number
}

function isToolLine(log: LogEntry): boolean {
  return log.detail?.toolType === 'tool_use' || log.detail?.toolType === 'tool_result'
}

// Groups consecutive tool_use/tool_result entries into a single collapsible
// "turn" (mirrors the Discord thread grouping in backend/discord_notifier.py's
// _format_stream_entries). Non-tool lines (assistant text, thinking, status)
// pass through individually and break the current turn.
const groupedLogs = computed<(LineItem | TurnItem)[]>(() => {
  const result: (LineItem | TurnItem)[] = []
  let turnNumber = 0
  let current: { log: LogEntry; index: number }[] = []

  const flush = () => {
    if (current.length === 0) return
    const counts: Record<string, number> = {}
    let toolCallCount = 0
    for (const { log } of current) {
      if (log.detail?.toolType === 'tool_use') {
        const name = (log.detail.name || 'tool').toLowerCase()
        counts[name] = (counts[name] || 0) + 1
        toolCallCount += 1
      }
    }
    // A single tool call carries no useful grouping information — showing its
    // own line (e.g. "▶ bash: pytest") is more informative than collapsing it
    // behind a generic "bash × 1 (1 tool call)" turn summary. toolCallCount can
    // also be 0 here (a run of tool_result entries with no tool_use), which
    // likewise needs no grouping and should just pass through as plain lines.
    if (toolCallCount <= 1) {
      for (const entry of current) {
        result.push({ type: 'line', log: entry.log, index: entry.index })
      }
      current = []
      return
    }
    turnNumber += 1
    result.push({ type: 'turn', turnNumber, entries: current, counts, toolCallCount })
    current = []
  }

  props.logs.forEach((log, index) => {
    if (isToolLine(log)) {
      current.push({ log, index })
    } else {
      flush()
      result.push({ type: 'line', log, index })
    }
  })
  flush()

  return result
})

function formatTurnCounts(counts: Record<string, number>): string {
  const parts = Object.entries(counts).map(([name, count]) => `${name} × ${count}`)
  return parts.length ? parts.join(', ') : 'tool activity'
}

function toggleTurn(turnNumber: number) {
  const next = new Set(expandedTurns.value)
  if (next.has(turnNumber)) next.delete(turnNumber)
  else next.add(turnNumber)
  expandedTurns.value = next
}

function toggleLineDetail(i: number) {
  expandedLineIdx.value = expandedLineIdx.value === i ? null : i
}

function toggleTurnLineDetail(i: number) {
  expandedTurnLineIdx.value = expandedTurnLineIdx.value === i ? null : i
}

defineExpose({
  bodyEl,
  reset: () => {
    expandedLineIdx.value = null
    expandedTurnLineIdx.value = null
    expandedTurns.value = new Set()
  },
})

watch(
  () => props.logs,
  async () => {
    await nextTick()
    if (bodyEl.value) bodyEl.value.scrollTop = bodyEl.value.scrollHeight
  },
  { deep: true },
)
</script>

<style scoped>
.pane-body {
  flex: 1;
  overflow-y: auto;
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.logs-empty {
  color: var(--color-text-dim);
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: 4px;
  font-style: italic;
  padding: 8px 0;
}

.cursor-blink {
  color: var(--color-amber);
  animation: blink 1s step-end infinite;
}

@keyframes blink {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0;
  }
}

.turn-group {
  display: flex;
  flex-direction: column;
}

.turn-summary {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--color-amber);
  cursor: pointer;
  border-radius: 2px;
}
.turn-summary:hover {
  background: rgba(255, 255, 255, 0.04);
}

.turn-summary-icon {
  font-size: 10px;
  flex-shrink: 0;
  opacity: 0.7;
}

.turn-summary-text {
  color: var(--color-text-dim);
}

.turn-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-left: 18px;
  padding-left: 8px;
  border-left: 2px solid #2a1a05;
}
</style>
