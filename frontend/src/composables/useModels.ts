import { ref, readonly } from 'vue'
import { useAuthStore } from '../stores/auth'

export interface ModelEntry {
  id: string
  name: string
}

export interface ProviderEntry {
  id: string
  name: string
  models: ModelEntry[]
}

const API_BASE = (import.meta.env.VITE_API_BASE as string) ?? ''

const providers = ref<ProviderEntry[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
let fetched = false

export function useModels() {
  const authStore = useAuthStore()

  async function loadModels() {
    if (fetched || loading.value) return
    loading.value = true
    error.value = null
    try {
      const res = await fetch(`${API_BASE}/api/models`, {
        headers: authStore.authHeaders(),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      providers.value = await res.json()
      fetched = true
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  function modelsForProvider(providerId: string): ModelEntry[] {
    const p = providers.value.find((p) => p.id === providerId)
    return p?.models ?? []
  }

  return {
    providers: readonly(providers),
    loading: readonly(loading),
    error: readonly(error),
    loadModels,
    modelsForProvider,
  }
}
