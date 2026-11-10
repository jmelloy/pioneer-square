type RGB = readonly [number, number, number]

// Body ramps from tools/sprites/robots.py, using each type's first atlas colourway.
const BODY_RAMPS: readonly (readonly RGB[])[] = [
  [
    [85, 49, 43],
    [138, 73, 48],
    [175, 89, 52],
    [209, 116, 59],
  ],
  [
    [48, 52, 48],
    [50, 88, 70],
    [54, 118, 88],
    [51, 189, 146],
  ],
  [
    [90, 71, 53],
    [151, 98, 54],
    [183, 123, 63],
    [228, 166, 68],
  ],
]

const EXTRA_RAMPS: readonly (readonly RGB[])[] = [
  [
    [32, 43, 64],
    [47, 73, 113],
    [66, 108, 155],
    [113, 161, 191],
  ], // cobalt
  [
    [52, 34, 55],
    [86, 51, 87],
    [126, 75, 117],
    [179, 121, 158],
  ], // plum
  [
    [58, 30, 36],
    [101, 43, 48],
    [151, 62, 57],
    [205, 105, 82],
  ], // crimson
  [
    [72, 64, 49],
    [118, 108, 82],
    [175, 163, 126],
    [222, 218, 165],
  ], // ivory
  [
    [28, 46, 48],
    [39, 77, 83],
    [51, 115, 125],
    [91, 170, 173],
  ], // petrol
  [
    [39, 46, 30],
    [65, 82, 43],
    [97, 122, 57],
    [151, 171, 91],
  ], // moss
]

export const ROBOT_COLORWAY_COUNT = 2 + EXTRA_RAMPS.length

/** Recolour only exact body shades; retain alpha, lamp markers and non-body materials. */
export function recolorBody(data: Uint8ClampedArray, type: number, colorway: number) {
  if (colorway < 2) return
  const source = BODY_RAMPS[type]
  const target = EXTRA_RAMPS[colorway - 2]
  for (let i = 0; i < data.length; i += 4) {
    if (data[i + 3] === 0) continue
    // Lanky's darkest body shade is shared with its metal/visor, so leave it alone.
    const shade = source.findIndex(
      ([r, g, b], index) =>
        !(type === 1 && index === 0) && data[i] === r && data[i + 1] === g && data[i + 2] === b,
    )
    if (shade < 0) continue
    ;[data[i], data[i + 1], data[i + 2]] = target[shade]
  }
}
