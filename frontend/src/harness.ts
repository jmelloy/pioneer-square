import { createApp } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import SpawnWorkerForm from './components/sidebar/SpawnWorkerForm.vue'
import { useAuthStore } from './stores/auth'
import { useGuildStore } from './stores/guild'
import { useGitHubStore } from './stores/github'
import './style.css'

const pinia = createPinia()
setActivePinia(pinia)

const origFetch = window.fetch.bind(window)
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = String(input)
  if (url.includes('/spawn-credentials')) {
    return new Response(
      JSON.stringify({
        guild_env_vars: [
          { key: 'ANTHROPIC_API_KEY', masked_value: 'sk…alue' },
          { key: 'AWS_PROFILE', masked_value: 'de…ult' },
        ],
        claude_credentials: { saved: true, updated_at: '2026-07-24T00:00:00Z' },
      }),
      { status: 200 },
    )
  }
  if (url.includes('/spawn-settings')) return new Response(JSON.stringify({}), { status: 200 })
  if (url.includes('/spawn-defaults')) return new Response(JSON.stringify({}), { status: 200 })
  return origFetch(input, init)
}

const app = createApp(SpawnWorkerForm)
app.use(pinia)

const authStore = useAuthStore()
authStore.user = { id: 'u1', login: 'demo' } as never
authStore.loginToken = 'tok'
const guildStore = useGuildStore()
guildStore.currentGuild = { id: 'g1', name: 'Demo Guild', primary_repo: null } as never
const ghStore = useGitHubStore()
ghStore.repos = [
  { full_name: 'acme/repo-one' },
  { full_name: 'acme/repo-two' },
  { full_name: 'other-org/widget' },
] as never

app.mount('#app')
