/// <reference types="node" />
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import close from '../scene.json'
import glued from '../scenes/glued/scene.json'
import { buildDepth } from '../depth'
import { makeGeometry } from '../scene'
import type { SceneConfig } from '../types'

describe('wide factory scene', () => {
  it('preserves all tile-space data and translates the existing geometry', () => {
    const { image: closeImage, projection: closeProjection, ...closeData } = close
    const { image, projection, view: _view, ...gluedData } = glued
    expect(gluedData).toEqual(closeData)
    expect(image).toEqual({ width: 854, height: 480 })
    expect(projection).toEqual({
      ...closeProjection,
      origin: { x: closeProjection.origin.x + 278, y: closeProjection.origin.y + 49 },
    })
    const closeGeo = makeGeometry(close as unknown as SceneConfig)
    const gluedGeo = makeGeometry(glued as unknown as SceneConfig)
    for (const [c, r] of closeGeo.listTiles('-WCB')) {
      const [x, y] = closeGeo.tileCentre(c, r)
      expect(gluedGeo.tileCentre(c, r)).toEqual([x + 278, y + 49])
    }
    expect(image.width).toBeGreaterThan(closeImage.width)
  })

  it('ships a background and occlusion mask matching the scene size', () => {
    for (const name of ['background', 'prop-mask']) {
      const png = readFileSync(`src/components/factory-floor/scenes/glued/assets/${name}.png`)
      expect([png.readUInt32BE(16), png.readUInt32BE(20)]).toEqual([
        glued.image.width,
        glued.image.height,
      ])
    }
  })

  it('preserves occlusion depth at the translated scene coordinates', () => {
    const { width: cw, height: ch } = close.image
    const { width: gw, height: gh } = glued.image
    const before = buildDepth(close as unknown as SceneConfig, new Uint8Array(cw * ch).fill(1))
    const after = buildDepth(glued as unknown as SceneConfig, new Uint8Array(gw * gh).fill(1))
    for (let y = 0; y < ch; y++) {
      expect(after.slice((y + 49) * gw + 278, (y + 49) * gw + 278 + cw)).toEqual(
        before.slice(y * cw, (y + 1) * cw),
      )
    }
  })
})
