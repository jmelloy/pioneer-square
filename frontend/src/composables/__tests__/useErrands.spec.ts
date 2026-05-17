import { describe, it, expect } from 'vitest'
import {
  type Poi,
  type Rng,
  buildErrandQueue,
  lingerFor,
  personalityFor,
  pickWeighted,
  poisFromGeometry,
} from '../useErrands'

const geom = {
  tableLeft: 28,
  tableWidth: 800,
  breakRoomTop: 400,
  breakRoomHeight: 64,
}

function seededRng(seed: number): Rng {
  let s = seed >>> 0
  return {
    next() {
      // Mulberry32 — small deterministic PRNG, good enough for tests.
      s = (s + 0x6d2b79f5) >>> 0
      let t = s
      t = Math.imul(t ^ (t >>> 15), t | 1)
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296
    },
  }
}

describe('useErrands · personalityFor', () => {
  it('returns the same personality for the same agent id', () => {
    expect(personalityFor('a-abc')).toEqual(personalityFor('a-abc'))
  })

  it('returns different weights for different agent ids', () => {
    const a = personalityFor('a-1')
    const b = personalityFor('a-different-id')
    expect(a).not.toEqual(b)
  })

  it('always assigns a positive weight to every POI kind', () => {
    for (const id of ['a-1', 'a-2', 'a-zzz', 'foreman']) {
      const p = personalityFor(id)
      for (const w of Object.values(p.weights)) expect(w).toBeGreaterThan(0)
    }
  })
})

describe('useErrands · poisFromGeometry', () => {
  it('produces the expected break-room destinations', () => {
    const pois = poisFromGeometry(geom)
    const kinds = pois.map((p) => p.kind)
    expect(kinds).toEqual(['bulletin', 'watercooler', 'couch', 'couch', 'table', 'table', 'table'])
  })

  it('places couch seats below the tables (higher y)', () => {
    const pois = poisFromGeometry(geom)
    const couch = pois.find((p) => p.kind === 'couch')!
    const table = pois.find((p) => p.kind === 'table')!
    expect(couch.pos.y).toBeGreaterThan(table.pos.y)
  })

  it('marks couch as sit, table as lean, others as stand', () => {
    const pois = poisFromGeometry(geom)
    expect(pois.find((p) => p.kind === 'couch')!.pose).toBe('sit')
    expect(pois.find((p) => p.kind === 'table')!.pose).toBe('lean')
    expect(pois.find((p) => p.kind === 'bulletin')!.pose).toBe('stand')
    expect(pois.find((p) => p.kind === 'watercooler')!.pose).toBe('stand')
  })

  it('scales POIs with break-room width', () => {
    const a = poisFromGeometry(geom)
    const b = poisFromGeometry({ ...geom, tableWidth: 1600 })
    const aWater = a.find((p) => p.kind === 'watercooler')!
    const bWater = b.find((p) => p.kind === 'watercooler')!
    expect(bWater.pos.x).toBeGreaterThan(aWater.pos.x)
  })
})

describe('useErrands · pickWeighted', () => {
  it('always picks the heaviest item when weights are extreme', () => {
    const rng = seededRng(42)
    const items = [
      { value: 'a', weight: 0.001 },
      { value: 'b', weight: 1000 },
    ]
    for (let i = 0; i < 50; i++) expect(pickWeighted(items, rng)).toBe('b')
  })

  it('returns each value at least once over enough draws when weights are equal', () => {
    const rng = seededRng(7)
    const items = ['a', 'b', 'c'].map((value) => ({ value, weight: 1 }))
    const seen = new Set<string>()
    for (let i = 0; i < 100; i++) seen.add(pickWeighted(items, rng))
    expect(seen).toEqual(new Set(['a', 'b', 'c']))
  })
})

describe('useErrands · lingerFor', () => {
  it('stays inside the lingerMs range', () => {
    const poi: Poi = {
      kind: 'couch',
      pos: { x: 0, y: 0 },
      lingerMs: [6000, 10000],
      pose: 'sit',
    }
    const rng = seededRng(1)
    for (let i = 0; i < 200; i++) {
      const v = lingerFor(poi, rng)
      expect(v).toBeGreaterThanOrEqual(6000)
      expect(v).toBeLessThan(10000)
    }
  })
})

describe('useErrands · buildErrandQueue', () => {
  const pois = poisFromGeometry(geom)
  const wanderFactory = (): Poi => ({
    kind: 'wander',
    pos: { x: 100, y: 100 },
    lingerMs: [1000, 2000],
    pose: 'stand',
  })

  it('produces 3..5 entries', () => {
    const rng = seededRng(123)
    for (let i = 0; i < 25; i++) {
      const q = buildErrandQueue(personalityFor(`a-${i}`), pois, wanderFactory, rng)
      expect(q.length).toBeGreaterThanOrEqual(3)
      expect(q.length).toBeLessThanOrEqual(5)
    }
  })

  it('does not place the same POI twice in a row', () => {
    const rng = seededRng(99)
    for (let i = 0; i < 50; i++) {
      const q = buildErrandQueue(personalityFor(`agent-${i}`), pois, wanderFactory, rng)
      for (let k = 1; k < q.length; k++) {
        const prev = q[k - 1]
        const cur = q[k]
        const same =
          prev.kind === cur.kind && prev.pos.x === cur.pos.x && prev.pos.y === cur.pos.y
        expect(same).toBe(false)
      }
    }
  })

  it('biases the chain toward kinds with higher personality weight', () => {
    // Synthetic personality that vastly prefers the couch.
    const p = {
      weights: { bulletin: 0.01, watercooler: 0.01, couch: 100, table: 0.01, wander: 0.01 },
    }
    const rng = seededRng(55)
    const counts: Record<string, number> = {}
    for (let i = 0; i < 30; i++) {
      const q = buildErrandQueue(p, pois, wanderFactory, rng)
      for (const poi of q) counts[poi.kind] = (counts[poi.kind] ?? 0) + 1
    }
    expect((counts.couch ?? 0)).toBeGreaterThan((counts.table ?? 0) + (counts.bulletin ?? 0))
  })
})
