/**
 * Idle-agent behavior tree: each agent gets a stable personality (hashed
 * from their id) that biases which break-room destinations they prefer.
 * When idle they work through a short errand chain — sit on the couch,
 * chat at a table, grab water, read the bulletin — instead of picking a
 * single random point.
 */

import type { Position } from './useAgentChoreography'

export type PoiKind = 'bulletin' | 'watercooler' | 'couch' | 'table' | 'wander'
export type AgentPose = 'stand' | 'sit' | 'lean'

export interface Poi {
  kind: PoiKind
  pos: Position
  /** Range of milliseconds to linger when arriving at this POI. */
  lingerMs: [number, number]
  pose: AgentPose
}

export interface Personality {
  weights: Record<PoiKind, number>
}

export interface BreakRoomGeometry {
  tableLeft: number
  tableWidth: number
  breakRoomTop: number
  breakRoomHeight: number
}

/** djb2-ish hash → unsigned 32-bit. Stable across runs. */
function hashId(id: string): number {
  let h = 5381
  for (let i = 0; i < id.length; i++) {
    h = ((h << 5) + h + id.charCodeAt(i)) >>> 0
  }
  return h
}

/**
 * Derive a stable personality from an agent id. Different bits of the
 * hash drive each preference so agents feel distinguishable.
 */
export function personalityFor(agentId: string): Personality {
  const h = hashId(agentId)
  // Each slice is 0..7. Add a small base so no weight is ever zero.
  const lazy = (h >>> 0) & 0x7
  const social = (h >>> 3) & 0x7
  const studious = (h >>> 6) & 0x7
  const restless = (h >>> 9) & 0x7
  const thirsty = (h >>> 12) & 0x7
  return {
    weights: {
      couch: 1 + lazy * 0.9,
      table: 1 + social * 0.7,
      bulletin: 0.6 + studious * 0.45,
      watercooler: 0.8 + thirsty * 0.35,
      wander: 0.5 + restless * 0.4,
    },
  }
}

/**
 * Build the static set of POIs derived from the break-room geometry.
 * Couch has two seats so two robots can sit at once without overlap.
 */
export function poisFromGeometry(geom: BreakRoomGeometry): Poi[] {
  const { tableLeft: tL, tableWidth: tW, breakRoomTop: brT, breakRoomHeight: brH } = geom
  const midY = brT + Math.round(brH * 0.45)
  const couchY = brT + brH - 14
  return [
    {
      kind: 'bulletin',
      pos: { x: tL + Math.round(tW * 0.04) + 18, y: midY },
      lingerMs: [3500, 5500],
      pose: 'stand',
    },
    {
      kind: 'watercooler',
      pos: { x: tL + tW - Math.round(tW * 0.04) - 18, y: midY },
      lingerMs: [1800, 3000],
      pose: 'stand',
    },
    {
      kind: 'couch',
      pos: { x: tL + Math.round(tW * 0.18) + 14, y: couchY },
      lingerMs: [6000, 10000],
      pose: 'sit',
    },
    {
      kind: 'couch',
      pos: { x: tL + Math.round(tW * 0.18) + 42, y: couchY },
      lingerMs: [6000, 10000],
      pose: 'sit',
    },
    {
      kind: 'table',
      pos: { x: tL + Math.round(tW * 0.33) + 21, y: midY },
      lingerMs: [2500, 5000],
      pose: 'lean',
    },
    {
      kind: 'table',
      pos: { x: tL + Math.round(tW * 0.49) + 21, y: midY },
      lingerMs: [2500, 5000],
      pose: 'lean',
    },
    {
      kind: 'table',
      pos: { x: tL + Math.round(tW * 0.65) + 21, y: midY },
      lingerMs: [2500, 5000],
      pose: 'lean',
    },
  ]
}

export interface Rng {
  /** Returns a float in [0, 1). */
  next(): number
}

export const defaultRng: Rng = { next: Math.random }

export function pickWeighted<T>(items: Array<{ value: T; weight: number }>, rng: Rng): T {
  const total = items.reduce((s, x) => s + x.weight, 0)
  let r = rng.next() * total
  for (const it of items) {
    r -= it.weight
    if (r <= 0) return it.value
  }
  return items[items.length - 1].value
}

export function lingerFor(poi: Poi, rng: Rng): number {
  const [lo, hi] = poi.lingerMs
  return lo + rng.next() * (hi - lo)
}

/**
 * Build a short errand chain (3..5 stops) of POIs weighted by personality.
 * `wanderFactory` produces a fresh wander POI on demand so it picks a new
 * random spot each time it's drawn (rather than the same one repeatedly).
 */
export function buildErrandQueue(
  personality: Personality,
  pois: Poi[],
  wanderFactory: () => Poi,
  rng: Rng = defaultRng,
): Poi[] {
  const length = 3 + Math.floor(rng.next() * 3)
  const queue: Poi[] = []
  for (let i = 0; i < length; i++) {
    const candidates: Array<{ value: Poi; weight: number }> = pois.map((p) => ({
      value: p,
      weight: personality.weights[p.kind] ?? 1,
    }))
    candidates.push({ value: wanderFactory(), weight: personality.weights.wander })
    const next = pickWeighted(candidates, rng)
    // Skip repeat of the immediately previous POI (boring to revisit instantly).
    const prev = queue[queue.length - 1]
    if (prev && prev.kind === next.kind && prev.pos.x === next.pos.x && prev.pos.y === next.pos.y) {
      i--
      continue
    }
    queue.push(next)
  }
  return queue
}
