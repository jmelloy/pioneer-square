import type { SceneConfig } from './types.ts'

/** Geometry helpers derived from a SceneConfig. Pure, no DOM. */
export function makeGeometry(scene: SceneConfig) {
  const ROWS = scene.grid.length
  const COLS = scene.grid[0].length
  const { tileWidth: tw, origin, lift } = scene.projection

  const kind = (c: number, r: number): string => scene.grid[r][c]
  const inBounds = (c: number, r: number) => c >= 0 && r >= 0 && c < COLS && r < ROWS
  /** Walkable = anything except outside floor "." and blocked "#". */
  const walkable = (c: number, r: number) =>
    inBounds(c, r) && kind(c, r) !== '.' && kind(c, r) !== '#'
  const isTableTile = (c: number, r: number) =>
    scene.tableTiles.some((t) => c >= t.c[0] && c <= t.c[1] && r >= t.r[0] && r <= t.r[1])

  /** Screen position (native px) of the centre of tile (c, r); fractional c, r interpolate. */
  const tileCentre = (c: number, r: number): [number, number] => [
    origin.x + ((c - r) * tw) / 2,
    origin.y + ((c + r + 1) * tw) / 4,
  ]
  /** Screen position of a floor point (c, r in tile units, no +0.5 offset) raised by `lift` px (bench top). */
  const benchTop = (c: number, r: number): [number, number] => [
    origin.x + ((c - r) * tw) / 2,
    origin.y + ((c + r) * tw) / 4 - lift,
  ]
  /** Inverse of benchTop for an arbitrary raise `l` (px): screen -> tile-space (c, r). */
  const screenToTile = (x: number, y: number, l: number): [number, number] => {
    const u = (x - origin.x) / (tw / 2),
      v = (y + l - origin.y) / (tw / 4)
    return [(u + v) / 2, (v - u) / 2]
  }
  /** Tiles whose grid character is in `chars`. */
  const listTiles = (chars: string): [number, number][] => {
    const out: [number, number][] = []
    for (let r = 0; r < ROWS; r++)
      for (let c = 0; c < COLS; c++) if (chars.includes(kind(c, r))) out.push([c, r])
    return out
  }

  return {
    ROWS,
    COLS,
    kind,
    inBounds,
    walkable,
    isTableTile,
    tileCentre,
    benchTop,
    screenToTile,
    listTiles,
  }
}
export type Geometry = ReturnType<typeof makeGeometry>

/** Direction vectors in atlas row order: SE, SW, NW, NE. */
export const DIRV: [number, number][] = [
  [1, 0],
  [0, 1],
  [-1, 0],
  [0, -1],
]
