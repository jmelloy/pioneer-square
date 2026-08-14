<template>
  <div class="modal-overlay" @click.self="$emit('close')">
    <div class="modal">
      <div class="modal-header">NEW THREAD</div>
      <div class="modal-body">
        <label class="field-label">Starting Message</label>
        <input
          v-model="message"
          class="field-input"
          placeholder="e.g. Can you look into the deploy pipeline failing?"
          @keydown.enter="onCreate"
          ref="messageInput"
        />
        <p class="field-hint">
          Sends this message to the Foreman, which creates a new conversation thread as a
          result. Discord is just one possible mirror of the conversation — not where it
          starts.
        </p>
      </div>
      <div class="modal-footer">
        <button class="pixel-btn cancel-btn" @click="$emit('close')">Cancel</button>
        <button class="pixel-btn" @click="onCreate" :disabled="creating || !message.trim()">
          {{ creating ? 'Creating...' : 'CREATE' }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, nextTick } from 'vue'

defineProps<{ creating: boolean }>()
const emit = defineEmits<{
  (e: 'close'): void
  (e: 'create', message: string): void
}>()

const message = ref('')
const messageInput = ref<HTMLInputElement | null>(null)

function onCreate() {
  const text = message.value.trim()
  if (!text) return
  emit('create', text)
}

onMounted(async () => {
  await nextTick()
  messageInput.value?.focus()
})
</script>

<style scoped>
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.7);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 200;
}

.modal {
  background: var(--color-bg-secondary);
  border: 3px solid var(--color-brass);
  box-shadow: 0 0 30px rgba(232, 170, 0, 0.3);
  width: 380px;
  display: flex;
  flex-direction: column;
}

.modal-header {
  font-family: var(--font-pixel);
  font-size: 9px;
  color: var(--color-brass-light);
  letter-spacing: 2px;
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
  font-size: 10px;
  color: var(--color-text-dim);
  letter-spacing: 1px;
}

.field-input {
  background: var(--color-bg);
  border: 2px solid var(--color-brass-dark);
  color: var(--color-text);
  font-family: var(--font-mono);
  font-size: 13px;
  padding: 8px 10px;
  outline: none;
  width: 100%;
}

.field-input:focus {
  border-color: var(--color-brass);
  box-shadow: 0 0 8px rgba(232, 170, 0, 0.3);
}

.field-input::placeholder {
  color: var(--color-text-dim);
}

.field-hint {
  font-size: 10px;
  color: var(--color-text-dim);
  line-height: 1.4;
  margin: 2px 0 0;
}

.modal-footer {
  display: flex;
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

@media (max-width: 600px) {
  .modal {
    width: calc(100vw - 32px);
  }
  .modal-footer {
    flex-wrap: wrap;
  }
}
</style>
