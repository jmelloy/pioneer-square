<template>
  <img
    v-if="src && !failed"
    :src="src"
    class="msg-avatar"
    alt=""
    referrerpolicy="no-referrer"
    @error="failed = true"
  />
  <span v-else class="msg-avatar msg-avatar--placeholder" aria-hidden="true">{{ initial }}</span>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

const props = defineProps<{ src?: string | null; name?: string | null }>()

// Missing URL or a broken image (deleted account, CDN hiccup) → initial badge.
const failed = ref(false)
watch(
  () => props.src,
  () => (failed.value = false),
)
const initial = computed(() => (props.name?.trim()[0] ?? '?').toUpperCase())
</script>

<style scoped>
.msg-avatar {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  flex-shrink: 0;
}

.msg-avatar--placeholder {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: var(--color-bg-secondary);
  color: var(--color-text-dim);
  font: 600 10px / 1 var(--font-ui);
}
</style>
