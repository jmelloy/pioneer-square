import type { SceneConfig } from './types.ts'
import { makeGeometry } from './scene.ts'

/**
 * Per-pixel occlusion depth for props.
 *
 * Every pixel that belongs to a prop stores the depth (column + row) of the tile
 * it stands on; -100 means "no prop". A robot pixel is hidden where the stored
 * depth is greater than the robot's depth, so edges are crisp and follow the
 * real prop shape.
 *
 * - Benches use their exact rectangles (scene.tables), raised by projection.lift.
 *   A bench pixel takes the depth of the footprint tile under it.
 * - Every other blocked tile is a box of height propHeights["c,r"] intersected
 *   with `propMask` (where prop pixels actually are in the background image).
 */
export const NO_PROP = -100

type Pt = [number, number]

function inConvex(p: Pt[], x: number, y: number): boolean {
  let s = 0
  for (let i = 0; i < p.length; i++) {
    const a = p[i],
      b = p[(i + 1) % p.length]
    const cr = (b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0])
    if (cr !== 0) {
      const g = cr > 0 ? 1 : -1
      if (s === 0) s = g
      else if (g !== s) return false
    }
  }
  return true
}

export function buildDepth(scene: SceneConfig, propMask: Uint8Array | null): Int16Array {
  const { width: W, height: H } = scene.image
  const geo = makeGeometry(scene)
  const { tileWidth: tw, origin, lift, thickness } = scene.projection
  const dep = new Int16Array(W * H).fill(NO_PROP)

  const hex = (c: number, r: number, h: number): Pt[] => {
    const hw = tw / 2,
      hh = tw / 4,
      tx = origin.x + (c - r) * hw,
      ty = origin.y + (c + r) * hh
    return [
      [tx - hw, ty + hh],
      [tx - hw, ty + hh - h],
      [tx, ty - h],
      [tx + hw, ty + hh - h],
      [tx + hw, ty + hh],
      [tx, ty + 2 * hh],
    ]
  }
  const raster = (poly: Pt[], d: number, mask: Uint8Array | null) => {
    let x0 = 1e9,
      y0 = 1e9,
      x1 = -1e9,
      y1 = -1e9
    for (const p of poly) {
      x0 = Math.min(x0, p[0])
      x1 = Math.max(x1, p[0])
      y0 = Math.min(y0, p[1])
      y1 = Math.max(y1, p[1])
    }
    for (let y = Math.max(0, Math.floor(y0)); y < Math.min(H, Math.ceil(y1)); y++)
      for (let x = Math.max(0, Math.floor(x0)); x < Math.min(W, Math.ceil(x1)); x++) {
        if (!inConvex(poly, x + 0.5, y + 0.5)) continue
        if (mask && !mask[y * W + x]) continue
        if (dep[y * W + x] < d) dep[y * W + x] = d
      }
  }

  // Generic props: box x prop mask.
  for (let r = 0; r < geo.ROWS; r++)
    for (let c = 0; c < geo.COLS; c++) {
      if (geo.kind(c, r) !== '#' || geo.isTableTile(c, r)) continue
      const h = scene.propHeights[c + ',' + r] ?? scene.defaultPropHeight
      if (h > 0) raster(hex(c, r, h), c + r, propMask)
    }

  // Benches: exact rectangles in tile space, top face `lift` px up, `thickness` px of slab.
  for (const T of scene.tables) {
    const q = [
      geo.benchTop(T.c0, T.r0),
      geo.benchTop(T.c1, T.r0),
      geo.benchTop(T.c1, T.r1),
      geo.benchTop(T.c0, T.r1),
    ]
    let x0 = 1e9,
      y0 = 1e9,
      x1 = -1e9,
      y1 = -1e9
    for (const p of q) {
      x0 = Math.min(x0, p[0])
      x1 = Math.max(x1, p[0])
      y0 = Math.min(y0, p[1])
      y1 = Math.max(y1, p[1] + thickness)
    }
    for (let y = Math.max(0, Math.floor(y0)); y < Math.min(H, Math.ceil(y1)); y++)
      for (let x = Math.max(0, Math.floor(x0)); x < Math.min(W, Math.ceil(x1)); x++) {
        let hit = false
        for (let l = lift - thickness; l <= lift; l++) {
          const t = geo.screenToTile(x + 0.5, y + 0.5, l)
          if (t[0] >= T.c0 && t[0] <= T.c1 && t[1] >= T.r0 && t[1] <= T.r1) {
            hit = true
            break
          }
        }
        if (!hit) continue
        const t = geo.screenToTile(x + 0.5, y + 0.5, lift)
        const cc = Math.min(T.c1 - 1e-6, Math.max(T.c0, t[0])),
          rr = Math.min(T.r1 - 1e-6, Math.max(T.r0, t[1]))
        const d = Math.floor(cc) + Math.floor(rr)
        if (dep[y * W + x] < d) dep[y * W + x] = d
      }
  }
  return dep
}

/**
 * Depth to use when testing a robot against the buffer. Robots working at, or
 * standing beside, a bench edge get a bonus so their upper bodies and arms are
 * not hidden by bench pixels they are physically above.
 */
export function robotDepth(
  scene: SceneConfig,
  b: { c: number; r: number; working: boolean },
): number {
  const { depthBonus, besideEdge, besideFarEnd } = scene.behavior
  const cx = b.c + 0.5,
    cy = b.r + 0.5
  let beside = false
  for (const T of scene.tables) {
    if (cy > T.r0 - 0.6 && cy < T.r1 + 0.6 && cx < T.c0 && cx > T.c0 - besideEdge) beside = true // along the left edge
    if (cx > T.c0 - 0.6 && cx < T.c1 + 0.6 && cy < T.r0 && cy > T.r0 - besideFarEnd) beside = true // past the far end
  }
  return b.c + b.r + 0.01 + (b.working || beside ? depthBonus : 0)
}
