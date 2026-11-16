import { nextTick, watch, type Ref } from 'vue'
import { useRoute, useRouter, type LocationQueryRaw } from 'vue-router'

// The query we've asked for but the router hasn't reached yet. Changes in the
// same tick (e.g. a tab switch that also flips the workspace) share one history
// entry, and later changes build on it instead of on the stale route.
let latest: LocationQueryRaw | null = null
let scheduled = false
// Two-way binds `target` to `?key=` on the current route. Local changes push a
// history entry; back/forward (route changes) write the URL value back into
// `target`. Missing or disallowed URL values read as `fallback`, and the
// fallback is omitted from the URL to keep links short.
export function useQueryRef(
  key: string,
  target: Ref<string>,
  fallback: string,
  allowed?: string[],
) {
  const route = useRoute()
  const router = useRouter()
  const fromRoute = () => {
    const value = route.query[key]
    return typeof value === 'string' && value && (!allowed || allowed.includes(value))
      ? value
      : fallback
  }

  target.value = fromRoute()
  watch(fromRoute, (value) => {
    target.value = value
  })
  watch(target, (value) => {
    const intended = latest && key in latest ? (latest[key] ?? fallback) : fromRoute()
    if (value === intended) return
    latest = { ...(latest ?? route.query), [key]: value === fallback ? undefined : value }
    if (scheduled) return
    scheduled = true
    nextTick(() => {
      scheduled = false
      const query = latest!
      router.push({ query }).finally(() => {
        if (latest === query) latest = null
      })
    })
  })
}
