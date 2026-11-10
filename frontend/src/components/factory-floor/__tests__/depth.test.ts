/// <reference types="node" />
import { test } from 'vitest'
import assert from 'node:assert/strict'
import sceneJson from '../scene.json'
import type { SceneConfig } from '../types.ts'
import { makeGeometry } from '../scene.ts'
import { buildDepth, robotDepth, NO_PROP } from '../depth.ts'

const scene = sceneJson as unknown as SceneConfig
const geo = makeGeometry(scene)
const W = scene.image.width
const mask = new Uint8Array(W * scene.image.height).fill(1)
const dep = buildDepth(scene, mask)
const at = ([x, y]: [number, number]) => dep[Math.round(y) * W + Math.round(x)]

test('bench pixels carry the depth of the footprint tile under them', () => {
  // top-face centre of footprint tile (2,4): floor point (2.5,4.5) raised by the lift
  assert.equal(at(geo.benchTop(2.5, 4.5)), 2 + 4)
  assert.equal(at(geo.benchTop(3.5, 6.5)), 3 + 6)
  assert.equal(at(geo.benchTop(6.5, 5.5)), 6 + 5)
})

test('open floor has no occluder', () => {
  assert.equal(at(geo.tileCentre(4, 5)), NO_PROP) // corridor between the benches
  assert.equal(at(geo.tileCentre(9, 9)), NO_PROP)
})

test('blocked prop tiles occlude at least with their own depth (overlapping boxes take the max)', () => {
  const [x, y] = geo.tileCentre(12, 6)
  assert.ok(dep[Math.round(y - 12) * W + Math.round(x)] >= 12 + 6) // well inside the 44 px box
})

test('the prop mask limits where boxes occlude', () => {
  const empty = buildDepth(scene, new Uint8Array(W * scene.image.height))
  const [x, y] = geo.tileCentre(12, 6)
  assert.equal(empty[Math.round(y - 12) * W + Math.round(x)], NO_PROP)
  // benches ignore the mask: they have exact geometry
  const t = geo.benchTop(2.5, 4.5)
  assert.equal(empty[Math.round(t[1]) * W + Math.round(t[0])], 2 + 4)
})

test('robots beside or working at a bench edge get the depth bonus', () => {
  const plain = robotDepth(scene, { c: 4, r: 5, working: false })
  assert.ok(Math.abs(plain - (9 + 0.01)) < 1e-9)
  const beside = robotDepth(scene, { c: 1, r: 5, working: false })
  assert.ok(Math.abs(beside - (6 + 0.01 + scene.behavior.depthBonus)) < 1e-9)
  const working = robotDepth(scene, { c: 8, r: 4, working: true })
  assert.ok(Math.abs(working - (12 + 0.01 + scene.behavior.depthBonus)) < 1e-9)
  // a robot at (1,3) is in front of the bench tile (2,3) it reaches over
  assert.ok(robotDepth(scene, { c: 1, r: 3, working: true }) > 2 + 3)
})
