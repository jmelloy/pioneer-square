<template>
  <button
    type="button"
    class="pixel-btn attach-btn"
    title="Attach image"
    aria-label="Attach image"
    @click="input?.click()"
  >
    📎
  </button>
  <input ref="input" type="file" accept="image/*" multiple hidden @change="onChange" />
</template>

<script setup lang="ts">
import { ref } from 'vue'

const emit = defineEmits<{ (e: 'files', files: File[]): void }>()
const input = ref<HTMLInputElement | null>(null)

function onChange() {
  const el = input.value
  if (!el) return
  if (el.files?.length) emit('files', Array.from(el.files))
  el.value = '' // allow re-selecting the same file
}
</script>

<style scoped>
.attach-btn {
  padding: 6px 8px;
  font-size: 14px;
  align-self: flex-start;
}
</style>
