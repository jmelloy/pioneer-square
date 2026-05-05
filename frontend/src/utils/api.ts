// Thin fetch wrapper: prepends API_BASE, attaches the auth header,
// and throws an Error with the server `detail` (or status code) on non-2xx.
//
// Usage:
//   const data = await api('/guilds')                       // GET, returns JSON
//   const data = await api('/guilds', { method: 'POST', json: { name: 'x' } })
//   await api('/foo', { method: 'DELETE', okStatuses: [404] })  // tolerate 404
import { useAuthStore } from '../stores/auth'

export const API_BASE = (import.meta.env.VITE_API_BASE as string) ?? ''

export interface ApiOptions extends Omit<RequestInit, 'body'> {
  json?: unknown
  okStatuses?: number[]
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
  }
}

export async function api<T = unknown>(path: string, opts: ApiOptions = {}): Promise<T> {
  const { json, okStatuses, headers, ...rest } = opts
  const auth = useAuthStore().authHeaders()
  const init: RequestInit = {
    ...rest,
    headers: {
      ...(json !== undefined ? { 'Content-Type': 'application/json' } : {}),
      ...auth,
      ...(headers as Record<string, string> | undefined),
    },
    ...(json !== undefined ? { body: JSON.stringify(json) } : {}),
  }
  const res = await fetch(`${API_BASE}${path}`, init)
  if (!res.ok && !okStatuses?.includes(res.status)) {
    let detail: { detail?: string } | null = null
    try {
      detail = await res.json()
    } catch {
      /* body may be empty or non-JSON */
    }
    throw new ApiError(detail?.detail ?? `HTTP ${res.status}`, res.status)
  }
  try {
    return (await res.json()) as T
  } catch {
    return undefined as T
  }
}
