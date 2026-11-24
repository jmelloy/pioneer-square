<template>
  <div class="ifp">
    <div class="ifp-head">
      <span class="ifp-title">⚙ Foreman</span>
    </div>

    <ul v-if="linked.length" class="ifp-convs">
      <li
        v-for="c in linked"
        :key="c.id"
        class="ifp-conv"
        :class="{ active: c.id === target?.id }"
        @click="targetId = c.id"
      >
        <span class="ifp-conv-name">{{ c.name || `Conversation #${c.id}` }}</span>
        <span class="ifp-conv-status">{{ c.status }}</span>
        <button class="ifp-open" title="Open conversation" @click.stop="open(c.id)">↗</button>
      </li>
    </ul>

    <div v-if="recent.length" class="ifp-activity">
      <div v-for="(m, i) in recent" :key="i" class="ifp-msg" :class="'from-' + sender(m)">
        <span class="ifp-msg-from">{{ sender(m) === 'user' ? 'You' : sender(m) }}</span>
        <span class="ifp-msg-text">{{ m.content }}</span>
      </div>
    </div>

    <form class="ifp-compose" @submit.prevent="send">
      <textarea
        v-model="text"
        class="ifp-input"
        rows="2"
        placeholder="Ask the foreman…"
        @keydown.enter.exact.prevent="send"
      ></textarea>
      <button class="pixel-btn ifp-send" type="submit" :disabled="!text.trim() || sending">
        {{ sending ? '…' : 'Ask Foreman' }}
      </button>
    </form>
    <div v-if="error" class="ifp-error">{{ error }}</div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useConversationsStore } from '../stores/conversations'
import { useGuildStore } from '../stores/guild'
import { useUiStore } from '../stores/ui'
import type { ChatMessage, Conversation, Task } from '../types'

const props = defineProps<{ owner: string; repo: string; issueNumber: number; tasks: Task[] }>()

const DEFAULT_TEXT = 'Ask Foreman'

const guildStore = useGuildStore()
const conversationsStore = useConversationsStore()
const uiStore = useUiStore()

// Bracketed so a search for [o/r#15] can't match [o/r#151]. Every message sent from
// here carries it, which is what links a conversation to the issue (no schema change).
const issueRef = computed(() => `[${props.owner}/${props.repo}#${props.issueNumber}]`)

const text = ref(DEFAULT_TEXT)
const sending = ref(false)
const error = ref('')
const searched = ref<Conversation[]>([])
const targetId = ref<number | null>(null)
const history = ref<ChatMessage[]>([])

// Conversations tied to this issue: ones whose messages mention the issue ref
// (server search), ones its tasks belong to, and live-created ones named with the ref.
const linked = computed<Conversation[]>(() => {
  const ids = new Set<number>(searched.value.map((c) => c.id))
  for (const t of props.tasks) {
    if (t.conversation_id != null) ids.add(t.conversation_id)
    for (const id of t.conversation_ids ?? []) ids.add(id)
  }
  for (const c of conversationsStore.conversations) {
    if (c.name?.includes(issueRef.value)) ids.add(c.id)
  }
  const byId = new Map(searched.value.map((c) => [c.id, c]))
  for (const c of conversationsStore.conversations) byId.set(c.id, c)
  return [...ids]
    .map((id) => byId.get(id))
    .filter((c): c is Conversation => !!c)
    .sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at))
    .slice(0, 3)
})

const target = computed(
  () =>
    linked.value.find((c) => c.id === targetId.value) ??
    linked.value.find((c) => c.status === 'active') ??
    linked.value[0] ??
    null,
)

function sender(m: ChatMessage): string {
  return String(m.from || m.from_agent || 'unknown')
}

// Last few plain chat lines of the target conversation: fetched history + live WS feed.
const recent = computed(() => {
  const id = target.value?.id
  if (id == null) return []
  const key = (m: ChatMessage) => `${sender(m)}|${m.content}`
  const seen = new Set(history.value.map(key))
  const live = guildStore.messages.filter(
    (m) => (m as ChatMessage).conversationId === id && !seen.has(key(m as ChatMessage)),
  ) as ChatMessage[]
  return [...history.value, ...live].filter((m) => !m.role && m.content).slice(-3)
})

watch(
  () => target.value?.id,
  async (id) => {
    history.value = []
    const guildId = guildStore.currentGuild?.id
    if (id == null || !guildId) return
    const page = await conversationsStore.fetchConversationMessages(guildId, id)
    if (target.value?.id === id) history.value = page
  },
  { immediate: true },
)

onMounted(async () => {
  const guildId = guildStore.currentGuild?.id
  if (!guildId) return
  try {
    searched.value = await conversationsStore.searchConversations(guildId, issueRef.value)
  } catch (e) {
    console.error('Failed to search issue conversations', e)
  }
})

function open(id: number) {
  uiStore.selectConversation(id)
  uiStore.openConversationTab(id)
}

async function send() {
  const body = text.value.trim()
  const guildId = guildStore.currentGuild?.id
  if (!body || !guildId || sending.value) return
  const content = `${issueRef.value} ${body}`
  error.value = ''
  sending.value = true
  try {
    if (!target.value) {
      // A top-level WS chat always starts a fresh conversation (backend ws_handlers.handle_chat),
      // named from this content, so it shows up in `linked` via the ref in its name.
      targetId.value = null
      if (!guildStore.sendMessage({ type: 'chat', from: 'user', to: 'foreman', content })) {
        throw new Error('Not connected — message not sent.')
      }
    } else {
      await conversationsStore.postConversationMessage(guildId, target.value.id, content)
    }
    text.value = ''
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    sending.value = false
  }
}
</script>

<style scoped>
.ifp {
  border-top: 2px solid var(--color-brass-dark);
  background: var(--color-bg-secondary);
  padding: 8px 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  flex-shrink: 0;
}
.ifp-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.ifp-title {
  font-family: var(--font-pixel);
  font-size: 9px;
  color: var(--color-brass-light);
  letter-spacing: 1px;
}
.ifp-convs {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.ifp-conv {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  padding: 2px 6px;
  cursor: pointer;
  border-left: 2px solid transparent;
}
.ifp-conv.active {
  border-left-color: var(--color-brass);
  background: rgba(255, 255, 255, 0.04);
}
.ifp-conv-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ifp-conv-status {
  color: var(--color-text-dim);
  font-size: 10px;
}
.ifp-open {
  background: none;
  border: none;
  color: var(--color-brass-light);
  cursor: pointer;
}
.ifp-activity {
  max-height: 96px;
  overflow-y: auto;
  font-size: 12px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.ifp-msg {
  display: flex;
  gap: 6px;
}
.ifp-msg-from {
  color: var(--color-brass-light);
  flex-shrink: 0;
}
.ifp-msg-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ifp-compose {
  display: flex;
  gap: 6px;
}
.ifp-input {
  flex: 1;
  background: var(--color-bg);
  border: 2px solid var(--color-brass-dark);
  color: var(--color-text);
  font-family: var(--font-mono);
  font-size: 12px;
  padding: 4px 8px;
  resize: vertical;
}
.ifp-input:focus {
  outline: none;
  border-color: var(--color-brass);
}
.ifp-error {
  color: var(--color-red, #e55);
  font-size: 11px;
}
</style>
