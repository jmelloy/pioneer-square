import { describe, expect, it } from 'vitest'
import { recolorBody, ROBOT_COLORWAY_COUNT } from '../colorways'

const bodyShades = [
  [138, 73, 48],
  [50, 88, 70],
  [151, 98, 54],
]
const secondColorwayShades = [
  [85, 49, 43],
  [63, 71, 59],
  [63, 71, 59],
]

describe('robot colourways', () => {
  it.each([0, 1])('leaves original colourway %i byte-for-byte unchanged', (colorway) => {
    for (let type = 0; type < 3; type++) {
      const pixels = new Uint8ClampedArray([...bodyShades[type], 255, 255, 0, 255, 255])
      const before = pixels.slice()
      recolorBody(pixels, type, colorway)
      expect(pixels).toEqual(before)
    }
  })

  it.each([0, 1, 2])('provides eight distinct shaded palettes for body type %i', (type) => {
    const colors = new Set<string>()
    for (let colorway = 0; colorway < ROBOT_COLORWAY_COUNT; colorway++) {
      const shade = colorway === 1 ? secondColorwayShades[type] : bodyShades[type]
      const pixels = new Uint8ClampedArray([...shade, 255])
      recolorBody(pixels, type, colorway)
      colors.add([...pixels].join(','))
    }
    expect(colors.size).toBe(8)
  })

  it.each([0, 1, 2])('preserves alpha, lamps, outlines and hardware for type %i', (type) => {
    const pixels = new Uint8ClampedArray([
      ...bodyShades[type],
      128,
      ...bodyShades[type],
      0,
      255,
      0,
      255,
      255,
      255,
      128,
      255,
      255,
      20,
      19,
      27,
      255,
      48,
      52,
      48,
      255,
      222,
      218,
      165,
      255,
    ])
    const before = pixels.slice()
    recolorBody(pixels, type, 2)
    expect([...pixels.slice(0, 4)]).toEqual([47, 73, 113, 128])
    expect(pixels.slice(4)).toEqual(before.slice(4))
  })

  it('keeps four distinct ordered body shades rather than flattening the shading', () => {
    const copper = [85, 49, 43, 255, 138, 73, 48, 255, 175, 89, 52, 255, 209, 116, 59, 255]
    for (let colorway = 2; colorway < ROBOT_COLORWAY_COUNT; colorway++) {
      const pixels = new Uint8ClampedArray(copper)
      recolorBody(pixels, 0, colorway)
      let previous = -1
      for (let i = 0; i < pixels.length; i += 4) {
        const brightness = pixels[i] + pixels[i + 1] + pixels[i + 2]
        expect(brightness).toBeGreaterThan(previous)
        previous = brightness
      }
    }
  })
})
