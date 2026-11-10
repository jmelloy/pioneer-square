import type { Robot, SceneConfig, Yaw } from './types.ts'
import { makeGeometry, DIRV } from './scene.ts'
import type { Geometry } from './scene.ts'

export type Rng = () => number

export function mulberry32(a: number): Rng {
  return () => {
    a |= 0
    a = (a + 0x6d2b79f5) | 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

/**
 * The robot simulation: wander between table work spots (work) and the coffee
 * machine / task board (break). Pure and deterministic given an Rng.
 */
export class Sim {
  robots: Robot[] = []
  readonly geo: Geometry
  scene: SceneConfig
  private rng: Rng
  constructor(scene: SceneConfig, rng: Rng = Math.random) {
    this.scene = scene
    this.rng = rng
    this.geo = makeGeometry(scene)
  }

  private rand(lo: number, hi: number) {
    return lo + this.rng() * (hi - lo)
  }
  private dwell(b: Robot) {
    const [lo, hi] =
      b.mode === 'work' ? this.scene.behavior.workDwell : this.scene.behavior.breakDwell
    return this.rand(lo, hi)
  }

  /** 4-neighbour BFS over walkable tiles. Returns tiles to step through (excluding the start), or null. */
  findPath(sc: number, sr: number, tc: number, tr: number): [number, number][] | null {
    const { COLS, ROWS, walkable } = this.geo
    if (sc === tc && sr === tr) return []
    const prev = new Int32Array(COLS * ROWS).fill(-2)
    const q = [sr * COLS + sc]
    prev[sr * COLS + sc] = -1
    for (let h = 0; h < q.length; h++) {
      const cur = q[h],
        c = cur % COLS,
        r = (cur - c) / COLS
      if (c === tc && r === tr) {
        const p: [number, number][] = []
        let k = cur
        while (prev[k] !== -1) {
          p.push([k % COLS, (k - (k % COLS)) / COLS])
          k = prev[k]
        }
        return p.reverse()
      }
      for (const [dc, dr] of DIRV) {
        const nc = c + dc,
          nr = r + dr
        if (walkable(nc, nr) && prev[nr * COLS + nc] === -2) {
          prev[nr * COLS + nc] = cur
          q.push(nr * COLS + nc)
        }
      }
    }
    return null
  }

  /** Which way a robot at (c, r) should face to look at the nearest bench tile. */
  yawToTable(c: number, r: number): Yaw {
    const { inBounds, isTableTile } = this.geo
    for (let k = 1; k <= 4; k++)
      for (let d = 0; d < 4; d++) {
        const cc = c + DIRV[d][0] * k,
          rr = r + DIRV[d][1] * k
        if (inBounds(cc, rr) && isTableTile(cc, rr)) return d as Yaw
      }
    return 1
  }

  spawn(n: number) {
    const tiles = this.geo.listTiles('-WCB')
    this.robots = []
    for (let i = 0; i < n; i++) {
      const t = tiles[Math.floor(this.rng() * tiles.length)]
      this.robots.push({
        c: t[0],
        r: t[1],
        path: [],
        wait: this.rng() * 2,
        mode: i % 2 ? 'break' : 'work',
        tc: -1,
        tr: -1,
        act: null,
        actT: 0,
        type: i % 3,
        cw: Math.floor(i / 3) % 2,
        seed: i * 0.37,
        yaw: 1,
        working: false,
      })
    }
  }

  private arrive(b: Robot) {
    b.wait = this.dwell(b)
    b.actT = 0
    const k = this.geo.kind(b.tc, b.tr)
    b.act = b.mode === 'break' ? (k === 'C' ? 'coffee' : k === 'B' ? 'board' : null) : null
    b.working = b.mode === 'work' && k === 'W'
    if (b.act) b.yaw = 3
    else if (b.working) b.yaw = this.yawToTable(b.tc, b.tr)
  }

  /** Choose the next destination for `b` given its mode. Returns false if unreachable. */
  private pick(b: Robot): boolean {
    const cand = this.geo.listTiles(b.mode === 'work' ? 'W' : 'CB')
    const taken = new Set(
      this.robots.filter((o) => o !== b && o.tc >= 0).map((o) => o.tc + ',' + o.tr),
    )
    const free = cand.filter((t) => !taken.has(t[0] + ',' + t[1]))
    const pool = free.length ? free : cand
    const t = pool[Math.floor(this.rng() * pool.length)]
    const p = this.findPath(Math.round(b.c), Math.round(b.r), t[0], t[1])
    if (!p) return false
    b.path = p
    b.tc = t[0]
    b.tr = t[1]
    if (!p.length) this.arrive(b)
    return true
  }

  step(dt: number) {
    const speed = this.scene.behavior.speed
    for (const b of this.robots) {
      if (b.path.length) {
        const t = b.path[0],
          dx = t[0] - b.c,
          dy = t[1] - b.r,
          d = Math.hypot(dx, dy),
          m = speed * dt
        b.yaw = (Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? 0 : 2) : dy > 0 ? 1 : 3) as Yaw
        if (d <= m) {
          b.c = t[0]
          b.r = t[1]
          b.path.shift()
          if (!b.path.length) this.arrive(b)
        } else {
          b.c += (dx / d) * m
          b.r += (dy / d) * m
        }
      } else {
        b.wait -= dt
        if (b.act || b.working) b.actT += dt
        if (b.wait <= 0) {
          b.act = null
          b.working = false
          if (b.tc === Math.round(b.c) && b.tr === Math.round(b.r))
            b.mode = b.mode === 'work' ? 'break' : 'work'
          if (!this.pick(b)) b.wait = 1
        }
      }
    }
  }
}
