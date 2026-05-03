<template>
  <div class="pane-header">
    <div class="pane-title">
      <span class="title-icon">{{ icon }}</span>
      <span class="title-text">{{ titleText }}</span>
    </div>
    <div class="pane-meta">
      <slot name="meta">
        <span v-if="entityState" class="state-badge" :class="entityState">{{ entityState }}</span>
        <span class="live-indicator" :class="{ active: isLive }">
          {{ isLive ? '● LIVE' : '○ IDLE' }}
        </span>
      </slot>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  icon: string
  titleText: string
  entityState?: string
}>()

const isLive = computed(() => {
  const s = props.entityState
  return !!s && !['idle', 'offline'].includes(s)
})
</script>

<style scoped>
.pane-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 16px;
  background: #0f0a02;
  border-bottom: 2px solid #2a1a05;
  flex-shrink: 0;
}

.pane-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}

.title-icon {
  color: var(--color-green);
}

.pane-meta {
  display: flex;
  align-items: center;
  gap: 12px;
}

.state-badge {
  font-size: 10px;
  padding: 2px 8px;
  border-radius: 2px;
  text-transform: uppercase;
  font-family: var(--font-pixel);
  letter-spacing: 1px;
}
.state-badge.idle {
  background: rgba(154, 128, 96, 0.2);
  color: var(--color-text-dim);
}
.state-badge.thinking {
  background: rgba(68, 153, 255, 0.2);
  color: var(--color-blue);
}
.state-badge.working {
  background: rgba(0, 255, 136, 0.2);
  color: var(--color-green);
}
.state-badge.busy {
  background: rgba(255, 136, 68, 0.2);
  color: var(--color-orange);
}
.state-badge.error {
  background: rgba(255, 51, 51, 0.2);
  color: var(--color-red);
}
.state-badge.offline {
  background: rgba(100, 100, 100, 0.2);
  color: var(--color-text-dim);
}

.live-indicator {
  font-size: 11px;
  color: var(--color-text-dim);
}

@keyframes livePulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.4;
  }
}
</style>
