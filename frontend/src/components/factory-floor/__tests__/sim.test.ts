/// <reference types="node" />
import { test } from 'vitest'
import assert from 'node:assert/strict'
import sceneJson from '../scene.json'
import type { SceneConfig } from '../types.ts'
import { Sim, mulberry32 } from '../sim.ts'

const scene = sceneJson as unknown as SceneConfig

function run(n: number, seconds: number, seed: number) {
  const sim = new Sim(scene, mulberry32(seed))
  sim.spawn(n)
  const seen = sim.robots.map(() => ({ work: 0, coffee: 0, board: 0 }))
  const dt = 1 / 30
  for (let i = 0; i < seconds * 30; i++) {
    sim.step(dt)
    sim.robots.forEach((b, k) => {
      const c = Math.round(b.c),
        r = Math.round(b.r)
      assert.ok(sim.geo.walkable(c, r), `robot ${k} at non-walkable tile ${c},${r}`)
      if (b.working) {
        assert.equal(b.mode, 'work')
        assert.equal(sim.geo.kind(c, r), 'W')
        seen[k].work++
      }
      if (b.act) {
        assert.equal(b.mode, 'break')
        assert.equal(b.yaw, 3)
        assert.equal(sim.geo.kind(c, r), b.act === 'coffee' ? 'C' : 'B')
        seen[k][b.act]++
      }
    })
  }
  return { sim, seen }
}

test('robots only stand on walkable tiles and acts match their tile', () => {
  run(8, 400, 1)
})

test('every robot eventually works, has coffee and visits the board', () => {
  const { seen } = run(6, 1500, 7)
  seen.forEach((s, k) => {
    assert.ok(s.work > 0, `robot ${k} never worked`)
    assert.ok(s.coffee + s.board > 0, `robot ${k} never took a break`)
  })
  assert.ok(seen.some((s) => s.coffee > 0) && seen.some((s) => s.board > 0))
})

test('no two robots work the same spot at once when spots are available', () => {
  const sim = new Sim(scene, mulberry32(3))
  sim.spawn(4)
  for (let i = 0; i < 30 * 600; i++) {
    sim.step(1 / 30)
    const targets = sim.robots.filter((b) => b.working).map((b) => b.tc + ',' + b.tr)
    assert.equal(new Set(targets).size, targets.length)
  }
})

test('same seed gives the same simulation', () => {
  const a = run(5, 120, 11).sim.robots.map((b) => [b.c, b.r, b.mode, b.act])
  const b = run(5, 120, 11).sim.robots.map((b) => [b.c, b.r, b.mode, b.act])
  assert.deepEqual(a, b)
})

test('paths are 4-connected, walkable, and end at the target', () => {
  const sim = new Sim(scene)
  const p = sim.findPath(0, 0, 8, 7)!
  let [c, r] = [0, 0]
  for (const [nc, nr] of p) {
    assert.equal(Math.abs(nc - c) + Math.abs(nr - r), 1)
    assert.ok(sim.geo.walkable(nc, nr))
    c = nc
    r = nr
  }
  assert.deepEqual([c, r], [8, 7])
})
