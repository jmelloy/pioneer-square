// Shared time formatters. Keep these tiny and pure — they show up everywhere.

// "w-vd3566" → "VD-3566", "w-x9" → "X-9"
export function formatWorkerId(workerId: string): string {
  const bare = workerId.replace(/^w-/, '')
  const m = bare.match(/\d/)
  if (m && m.index !== undefined && m.index > 0) {
    return `${bare.slice(0, m.index).toUpperCase()}-${bare.slice(m.index).toUpperCase()}`
  }
  return bare.toUpperCase()
}

export function formatClock(iso?: string, withSeconds = false): string {
  if (!iso) return withSeconds ? '00:00:00' : ''
  const d = new Date(iso)
  return withSeconds
    ? d.toLocaleTimeString('en-US', { hour12: false })
    : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

// "just now / 5m ago / 2h ago / 1/4/2025"
export function formatRelative(iso?: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  const diffMins = Math.floor((Date.now() - d.getTime()) / 60000)
  if (diffMins < 1) return 'just now'
  if (diffMins < 60) return `${diffMins}m ago`
  const diffHours = Math.floor(diffMins / 60)
  if (diffHours < 24) return `${diffHours}h ago`
  return d.toLocaleDateString()
}

// "5m / 2h / 3d" — compact, no "ago", days don't fall back to locale date.
export function formatAge(iso?: string): string {
  if (!iso) return ''
  const diffMins = Math.floor((Date.now() - new Date(iso).getTime()) / 60000)
  if (diffMins < 60) return `${diffMins}m`
  const diffHours = Math.floor(diffMins / 60)
  if (diffHours < 24) return `${diffHours}h`
  return `${Math.floor(diffHours / 24)}d`
}
