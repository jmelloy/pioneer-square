<template>
  <div class="pane-body" ref="bodyEl">
    <div v-if="visibleLogs.length === 0" class="logs-empty">
      <span class="cursor-blink">_</span> Waiting for output…
    </div>
    <template
      v-for="item in groupedLogs"
      :key="
        item.type === 'event'
          ? item.key
          : item.type === 'turn'
            ? `turn-${item.turnNumber}`
            : `line-${item.index}`
      "
    >
      <LogLine
        v-if="item.type === 'line'"
        :log="item.log"
        :expanded="expandedLineIdx === item.index"
        @toggle="toggleLineDetail(item.index)"
      />
      <div v-else-if="item.type === 'event'" class="event-group">
        <button
          type="button"
          class="turn-summary event-summary"
          :aria-expanded="expandedEvents.has(item.key)"
          @click="toggleEvent(item.key)"
        >
          <span class="turn-summary-icon">{{ expandedEvents.has(item.key) ? '▾' : '▸' }}</span>
          <span class="turn-summary-text">
            {{ item.summary }}
            <template v-if="item.entries.length > 1"> ({{ item.entries.length }} updates)</template>
          </span>
        </button>
        <div v-if="expandedEvents.has(item.key)" class="turn-body">
          <LogLine
            v-for="entry in item.entries"
            :key="entry.index"
            :log="entry.log"
            :expanded="expandedTurnLineIdx === entry.index"
            @toggle="toggleTurnLineDetail(entry.index)"
          />
        </div>
      </div>
      <div v-else class="turn-group">
        <div class="turn-summary" @click="toggleTurn(item.turnNumber)">
          <span class="turn-summary-icon">{{
            expandedTurns.has(item.turnNumber) ? '▾' : '▸'
          }}</span>
          <span class="turn-summary-text">
            Turn {{ item.turnNumber }} — {{ formatTurnCounts(item.counts) }} ({{
              item.toolCallCount
            }}
            tool call{{ item.toolCallCount === 1 ? '' : 's' }})
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
import { isVisibleLogEntry } from '../../utils/logs'
import { compactClaudeLogs, type ClaudeLogGroup } from '../../utils/compactClaudeLogs'

const props = defineProps<{ logs: LogEntry[] }>()

const bodyEl = ref<HTMLElement | null>(null)
// Separate expansion state for top-level lines vs. lines nested inside a turn
// body: both index into the same props.logs array, so sharing one ref would
// let expanding a line in one context incorrectly expand/collapse the entry
// at the same index in the other context.
const expandedLineIdx = ref<number | null>(null)
const expandedTurnLineIdx = ref<number | null>(null)
const expandedTurns = ref<Set<number>>(new Set())
const expandedEvents = ref<Set<string>>(new Set())

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
  if (log.detail?.is_error === true) return false
  // 'thinking' blocks routinely appear between tool calls in the same logical
  // turn (Claude emits thinking → tool_use → tool_result per step), so they're
  // treated as part of the run rather than a boundary — otherwise every
  // tool call in a multi-step turn gets its own thinking-separated fragment
  // and never accumulates past toolCallCount 1, defeating grouping entirely.
  return (
    log.detail?.toolType === 'tool_use' ||
    log.detail?.toolType === 'tool_result' ||
    log.detail?.toolType === 'thinking'
  )
}

// Groups consecutive tool_use/tool_result/thinking entries into a single
// collapsible "turn" (mirrors the Discord thread grouping in
// backend/discord_notifier.py's _format_stream_entries). Non-tool lines
// (assistant text, status) pass through individually and break the current
// turn.
const visibleLogs = computed(() =>
  props.logs.map((log, index) => ({ log, index })).filter(({ log }) => isVisibleLogEntry(log)),
)

const groupedLogs = computed<(LineItem | TurnItem | ClaudeLogGroup)[]>(() => {
  const result: (LineItem | TurnItem | ClaudeLogGroup)[] = []
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

  for (const item of compactClaudeLogs(visibleLogs.value)) {
    if ('type' in item) {
      flush()
      result.push(item)
    } else if (isToolLine(item.log)) {
      current.push(item)
    } else {
      flush()
      result.push({ type: 'line', ...item })
    }
  }
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

function toggleEvent(key: string) {
  const next = new Set(expandedEvents.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  expandedEvents.value = next
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
    expandedEvents.value = new Set()
  },
})

// How close to the bottom (in px) counts as "at the bottom" for auto-scroll purposes.
const BOTTOM_THRESHOLD = 48

function isNearBottom(): boolean {
  const el = bodyEl.value
  if (!el) return true
  return el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_THRESHOLD
}

watch(
  () => props.logs,
  async () => {
    // Capture scroll position before new entries are rendered, so it reflects
    // whether the user was at the bottom prior to this update rather than after
    // (once new content lands, the pre-update scrollTop no longer looks "at bottom").
    const shouldStickToBottom = isNearBottom()
    await nextTick()
    if (shouldStickToBottom && bodyEl.value) {
      bodyEl.value.scrollTop = bodyEl.value.scrollHeight
    }
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
.event-summary {
  background: none;
  border: 0;
  padding: 0;
  font-family: inherit;
  text-align: left;
  width: 100%;
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
