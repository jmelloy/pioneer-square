<template>
  <p v-if="!messages.length" class="empty">No messages yet</p>
  <ol v-else class="feed">
    <li v-for="(msg, i) in messages" :key="msg.id != null ? String(msg.id) : i" class="feed-item">
      <span class="avatar" aria-hidden="true">{{ sender(msg).charAt(0).toUpperCase() }}</span>
      <div class="feed-body">
        <div class="feed-head">
          <span class="from">{{ sender(msg) }}</span>
          <span class="time">{{ formatRelative(msg.created_at ?? msg.createdAt) }}</span>
        </div>
        <p class="content">{{ msg.content }}</p>
      </div>
    </li>
  </ol>
</template>

<script setup lang="ts">
import { formatRelative } from '../../utils/format'
import type { ChatMessage } from '../../types'

defineProps<{ messages: ChatMessage[] }>()

function sender(msg: ChatMessage): string {
  return String(msg.from || msg.from_agent || 'unknown')
}
</script>

<style scoped>
.feed {
  list-style: none;
}
.feed-item {
  display: flex;
  gap: 14px;
  padding: 14px;
  border-bottom: 1px solid var(--color-bg-tertiary);
}
.feed-item:last-child {
  border-bottom: none;
}
.avatar {
  flex-shrink: 0;
  width: 40px;
  height: 40px;
  display: grid;
  place-items: center;
  border: 2px solid var(--color-brass);
  border-radius: 50%;
  background: radial-gradient(circle at 35% 30%, #5a4ad0, #241860);
  color: var(--color-text);
  font-family: var(--font-pixel);
  font-size: 12px;
}
.feed-body {
  min-width: 0;
}
.feed-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.from {
  color: var(--color-brass-light);
  font-weight: bold;
  font-size: 15px;
}
.time {
  color: var(--color-text-dim);
  font-size: 13px;
}
.content {
  margin-top: 4px;
  font-size: 14px;
  line-height: 1.5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.empty {
  padding: 14px;
  color: var(--color-text-dim);
  font-size: 13px;
}
</style>
