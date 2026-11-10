/// <reference types="node" />
import { test } from 'vitest'
import assert from 'node:assert/strict'
import sceneJson from '../scene.json'
import type { SceneConfig } from '../types.ts'
import { makeGeometry } from '../scene.ts'
import { Sim } from '../sim.ts'

const scene = sceneJson as unknown as SceneConfig
const geo = makeGeometry(scene)

test('grid is rectangular and only uses known characters', () => {
  for (const row of scene.grid) assert.equal(row.length, geo.COLS)
  const known = new Set(Object.keys(scene.legend))
  for (const row of scene.grid)
    for (const ch of row) assert.ok(known.has(ch), `unknown grid char ${ch}`)
})

test('all walkable tiles form one connected region', () => {
  const tiles = geo.listTiles('-WCB')
  const seen = new Set<string>([tiles[0].join(',')])
  const q = [tiles[0]]
  for (let i = 0; i < q.length; i++) {
    const [c, r] = q[i]
    for (const [dc, dr] of [
      [1, 0],
      [-1, 0],
      [0, 1],
      [0, -1],
    ]) {
      const n: [number, number] = [c + dc, r + dr]
      if (geo.walkable(n[0], n[1]) && !seen.has(n.join(','))) {
        seen.add(n.join(','))
        q.push(n)
      }
    }
  }
  assert.equal(seen.size, tiles.length)
})

test('every work / coffee / board spot is reachable from every walkable tile', () => {
  const sim = new Sim(scene)
  const targets = geo.listTiles('WCB')
  for (const [sc, sr] of geo.listTiles('-WCB'))
    for (const [tc, tr] of targets)
      assert.ok(sim.findPath(sc, sr, tc, tr), `no path (${sc},${sr}) -> (${tc},${tr})`)
})

test('spots match the W tiles, sit on a bench, and face it', () => {
  const w = geo
    .listTiles('W')
    .map((t) => t.join(','))
    .sort()
  assert.deepEqual(scene.spots.map((s) => s.tile.join(',')).sort(), w)
  const sim = new Sim(scene)
  for (const s of scene.spots) {
    const [x, y] = s.doohickey.pos
    assert.ok(
      scene.tables.some((T) => x >= T.c0 && x <= T.c1 && y >= T.r0 && y <= T.r1),
      `doohickey ${s.tile} not on a bench`,
    )
    assert.ok([0, 1, 2].includes(s.doohickey.type))
    const yaw = sim.yawToTable(s.tile[0], s.tile[1])
    const [dc, dr] = [
      [1, 0],
      [0, 1],
      [-1, 0],
      [0, -1],
    ][yaw]
    let found = false
    for (let k = 1; k <= 4; k++)
      if (
        geo.inBounds(s.tile[0] + dc * k, s.tile[1] + dr * k) &&
        geo.isTableTile(s.tile[0] + dc * k, s.tile[1] + dr * k)
      )
        found = true
    assert.ok(found, `spot ${s.tile} does not face a bench`)
  }
})

test('prop heights only refer to blocked, non-bench tiles', () => {
  for (const k of Object.keys(scene.propHeights)) {
    const [c, r] = k.split(',').map(Number)
    assert.equal(geo.kind(c, r), '#', `${k} is not a blocked tile`)
    assert.ok(!geo.isTableTile(c, r), `${k} is a bench tile (benches use exact geometry)`)
  }
})

test('bench footprints are inside the grid', () => {
  for (const T of scene.tables) {
    assert.ok(T.c0 >= 0 && T.c1 <= geo.COLS && T.r0 >= 0 && T.r1 <= geo.ROWS)
    assert.ok(T.c1 > T.c0 && T.r1 > T.r0)
  }
})
