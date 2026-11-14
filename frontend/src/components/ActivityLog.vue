<template>
  <BaseCard title="Activity">
    <p v-if="!events.length" class="empty">No recent activity</p>
    <ol v-else class="events">
      <li v-for="(e, i) in events" :key="i" class="event" :class="'level-' + (e.level || 'info')">
        <span class="time">{{ formatRelative(e.timestamp) }}</span>
        <button class="who" @click="emit('select-agent', e.agentId)">{{ e.agentName }}</button>
        <span class="line">{{ e.line }}</span>
      </li>
    </ol>
  </BaseCard>
</template>

<script setup lang="ts">
import BaseCard from './BaseCard.vue'
import { formatRelative } from '../utils/format'
import type { LogEntry } from '../types'

export interface ActivityEvent extends LogEntry {
  agentId: string
  agentName: string
}

defineProps<{ events: ActivityEvent[] }>()
const emit = defineEmits<{ 'select-agent': [id: string] }>()
</script>

<style scoped>
.events {
  list-style: none;
}
.event {
  display: grid;
  grid-template-columns: auto auto 1fr;
  gap: 8px;
  align-items: baseline;
  padding: 5px 12px;
  border-bottom: 1px solid var(--color-bg-tertiary);
  font-size: 12px;
}
.time {
  font-size: 10px;
  color: var(--color-text-dim);
  white-space: nowrap;
}
.who {
  background: none;
  border: none;
  padding: 0;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--color-teal);
  cursor: pointer;
}
.who:hover {
  text-decoration: underline;
}
.line {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.level-thinking .line {
  color: var(--color-text-dim);
}
.empty {
  padding: 12px;
  font-size: 12px;
  color: var(--color-text-dim);
}
</style>
