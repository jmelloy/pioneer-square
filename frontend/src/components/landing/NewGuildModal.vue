<template>
  <dialog
    ref="dialog"
    class="modal-overlay ui-surface"
    aria-labelledby="new-guild-title"
    @click.self="$emit('close')"
    @cancel.prevent="$emit('close')"
  >
    <div class="modal">
      <div id="new-guild-title" class="modal-header">NEW GUILD</div>
      <div class="modal-body">
        <label class="field-label" for="new-guild-name">Guild Name (optional)</label>
        <input
          id="new-guild-name"
          v-model="name"
          class="field-input"
          placeholder="e.g. Feature Sprint #4"
          @keydown.enter="$emit('create', name)"
          ref="nameInput"
        />
      </div>
      <div class="modal-footer">
        <button class="pixel-btn cancel-btn" @click="$emit('close')">Cancel</button>
        <button class="pixel-btn" @click="$emit('create', name)" :disabled="creating">
          {{ creating ? 'Creating...' : 'CREATE' }}
        </button>
      </div>
    </div>
  </dialog>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, nextTick } from 'vue'

defineProps<{ creating: boolean }>()
defineEmits<{
  (e: 'close'): void
  (e: 'create', name: string): void
}>()

const dialog = ref<HTMLDialogElement | null>(null)
const name = ref('')
const nameInput = ref<HTMLInputElement | null>(null)

onMounted(async () => {
  dialog.value?.showModal()
  await nextTick()
  nameInput.value?.focus()
})
onBeforeUnmount(() => dialog.value?.close())
</script>

<style scoped>
.modal-overlay {
  margin: 0;
  border: 0;
  width: 100%;
  max-width: none;
  height: 100dvh;
  max-height: none;
  padding: max(16px, env(safe-area-inset-top)) max(16px, env(safe-area-inset-right))
    max(16px, env(safe-area-inset-bottom)) max(16px, env(safe-area-inset-left));
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.7);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 200;
}

.modal-overlay::backdrop {
  background: transparent;
}

.modal {
  max-width: 100%;
  max-height: 100%;
  overflow-y: auto;
  background: var(--color-bg-secondary);
  border: 3px solid var(--color-brass);
  box-shadow: 0 0 30px rgba(232, 170, 0, 0.3);
  width: 380px;
  display: flex;
  flex-direction: column;
}

.modal-header {
  font: 400 var(--text-section-title) / var(--leading-heading) var(--font-display);
  color: var(--color-brass-light);
  overflow-wrap: anywhere;
  padding: 14px 18px;
  background: var(--color-bg-tertiary);
  border-bottom: 2px solid var(--color-brass-dark);
}

.modal-body {
  padding: 20px 18px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.field-label {
  font: 600 var(--text-small) / var(--leading-heading) var(--font-ui);
  color: var(--color-text-dim);
}

.field-input {
  background: var(--color-bg);
  border: 2px solid var(--color-brass-dark);
  color: var(--color-text);
  font: var(--text-input) / 1.5 var(--font-ui);
  min-width: 0;
  padding: 8px 10px;
  width: 100%;
}

.field-input:focus {
  border-color: var(--color-brass);
  box-shadow: 0 0 8px rgba(232, 170, 0, 0.3);
}

.field-input::placeholder {
  color: var(--color-text-dim);
}

.modal-footer {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 10px;
  padding: 14px 18px;
  border-top: 2px solid var(--color-brass-dark);
  background: var(--color-bg-tertiary);
}

.cancel-btn {
  background: transparent;
  border-color: var(--color-brass-dark);
  color: var(--color-text-dim);
}

.cancel-btn:hover {
  background: rgba(255, 255, 255, 0.05);
  box-shadow: none;
}
</style>
