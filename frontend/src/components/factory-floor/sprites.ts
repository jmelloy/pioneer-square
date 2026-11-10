/**
 * Robot sprite engine (browser only: needs canvas). Importing this file has no side effects.
 *
 * Atlas layout (robots-atlas.png): one block of 4 rows per (type, colourway):
 *   rowBlock = (type * 2 + colourway) * 4,  row = rowBlock + yaw  (yaw 0 SE, 1 SW, 2 NW, 3 NE)
 * Columns are frames, in the order given by `meta.anims[anim].off + frame`.
 * Six extra body palettes are derived from colourway 0; the atlas itself is unchanged.
 * Lamp pixels are baked as marker colours and recoloured here per mode.
 */
import type { Robot, RobotAtlasMeta } from './types.ts'
import { recolorBody } from './colorways'

type RGB = [number, number, number]
const LAMP_MODES: Record<string, { main: RGB; hi: RGB }> = {
  work: { main: [82, 219, 163], hi: [222, 218, 165] }, // palette teal (the window's glow)
  break: { main: [235, 191, 68], hi: [227, 218, 95] }, // palette amber
  busy: { main: [227, 218, 95], hi: [222, 218, 165] }, // flashes on every strike frame
}
const NOTE_COLORS = ['#e3da5f', '#52dba3', '#d1743b']

export function pickFrame(b: Robot, tm: number): [string, number] {
  if (b.path.length) return ['walk', Math.floor(tm * 8 + b.seed * 3) % 4]
  const t = b.actT
  if (b.act === 'coffee') {
    const c = t % 3.2
    return ['coffee', c < 0.8 ? 0 : c < 1.8 ? 1 : c < 2.8 ? 2 : 3]
  }
  if (b.act === 'board') {
    const c = t % 3.4
    return ['board', c < 0.9 ? 0 : c < 2.0 ? (Math.floor(t * 5) % 2 ? 1 : 2) : c < 2.9 ? 3 : 0]
  }
  if (b.working) return ['work', Math.floor(tm * 4 + b.seed * 3) % 4]
  return ['idle', Math.floor(tm * 1.5 + b.seed * 3) % 2]
}

export class RobotSprites {
  readonly meta: RobotAtlasMeta
  private variants = new Map<string, HTMLCanvasElement>()
  private scratch: HTMLCanvasElement
  private sg: CanvasRenderingContext2D
  private atlas: HTMLImageElement

  constructor(atlas: HTMLImageElement, meta: RobotAtlasMeta) {
    this.atlas = atlas
    this.meta = meta
    this.scratch = document.createElement('canvas')
    this.scratch.width = meta.FW
    this.scratch.height = meta.FH
    this.sg = this.scratch.getContext('2d', { willReadFrequently: true })!
  }

  /** Cache only variants actually used, rather than allocating all eight palettes upfront. */
  private variant(type: number, colorway: number, mode: string) {
    const key = `${type}|${colorway}|${mode}`
    const cached = this.variants.get(key)
    if (cached) return cached
    const { FH } = this.meta
    const c = document.createElement('canvas')
    c.width = this.atlas.width
    c.height = 4 * FH
    const g = c.getContext('2d', { willReadFrequently: true })!
    const sourceColorway = colorway === 1 ? 1 : 0
    g.drawImage(
      this.atlas,
      0,
      (type * 2 + sourceColorway) * 4 * FH,
      c.width,
      c.height,
      0,
      0,
      c.width,
      c.height,
    )
    const pixels = g.getImageData(0, 0, c.width, c.height)
    const d = pixels.data
    recolorBody(d, type, colorway)
    const lamp = LAMP_MODES[mode]
    for (let i = 0; i < d.length; i += 4) {
      if (d[i + 3] === 0 || d[i] !== 255 || d[i + 2] !== 255) continue
      const q = d[i + 1] === 0 ? lamp.main : d[i + 1] === 128 ? lamp.hi : null
      if (q) [d[i], d[i + 1], d[i + 2]] = q
    }
    g.putImageData(pixels, 0, 0)
    this.variants.set(key, c)
    return c
  }

  /** Things in the robot's hand / impact sparks. Drawn into the sprite layer so props can still hide them. */
  private overlays(an: string, f: number, h: [number, number], tm: number, i: number) {
    const R = (x: number, y: number, w: number, hh: number, c: string) => {
      this.sg.fillStyle = c
      this.sg.fillRect(x, y, w, hh)
    }
    const [hx, hy] = h
    if (an === 'coffee' && f > 0) {
      R(hx - 2, hy - 6, 6, 6, '#14131b')
      R(hx - 1, hy - 5, 4, 4, '#dedaa5')
      R(hx - 1, hy - 5, 4, 1, '#55312b')
      R(hx + 4, hy - 5, 2, 3, '#14131b')
      const w = Math.floor((tm * 5 + i) % 3)
      R(hx - 1 + (w === 1 ? 1 : 0), hy - 9 - w, 1, 2, 'rgba(222,218,165,.7)')
      R(hx + 2 - (w === 2 ? 1 : 0), hy - 10 - ((w + 1) % 3), 1, 2, 'rgba(222,218,165,.45)')
    } else if (an === 'board' && f === 3) {
      R(hx - 3, hy - 8, 8, 8, '#14131b')
      R(hx - 2, hy - 7, 6, 6, NOTE_COLORS[i % 3])
      R(hx - 1, hy - 5, 4, 1, 'rgba(20,19,27,.45)')
      R(hx - 1, hy - 3, 3, 1, 'rgba(20,19,27,.45)')
    } else if (an === 'work' && (f === 1 || f === 3)) {
      R(hx - 2, hy - 1, 5, 1, '#e3da5f')
      R(hx, hy - 3, 1, 5, '#e3da5f')
      R(hx, hy - 1, 1, 1, '#dedaa5')
    }
  }

  /**
   * Draw one robot with feet at screen position (px, py), hiding pixels where `depth`
   * (the prop depth buffer, W x H) is deeper than `robotDepth`.
   */
  draw(
    ctx: CanvasRenderingContext2D,
    b: Robot,
    i: number,
    tm: number,
    px: number,
    py: number,
    depth: Int16Array,
    W: number,
    H: number,
    robotDepth: number,
  ) {
    const { FW, FH, AX, AY } = this.meta,
      sg = this.sg
    const tp = this.meta.types[b.type]
    const [an, f] = pickFrame(b, tm)
    const fi = this.meta.anims[an].off + f
    sg.clearRect(0, 0, FW, FH)
    // ground shadow
    const [sw, sh] = this.meta.shadow[tp],
      hh = Math.max(2, Math.round(sh * 0.8))
    sg.fillStyle = 'rgba(20,19,27,.45)'
    for (let dy = -hh; dy <= hh; dy++) {
      const half = Math.floor(sw * Math.sqrt(1 - (dy * dy) / ((hh + 0.5) * (hh + 0.5))))
      sg.fillRect(AX - half, AY + dy - 1, 2 * half + 1, 1)
    }
    const lamp = an === 'work' && (f === 1 || f === 3) ? 'busy' : b.mode // lamps flash bright on every strike
    sg.drawImage(this.variant(b.type, b.cw, lamp), fi * FW, b.yaw * FH, FW, FH, 0, 0, FW, FH)
    this.overlays(an, f, this.meta.anchors[tp][this.meta.dirs[b.yaw]][fi], tm, i)
    // per-pixel depth test against props
    const bx = px - AX,
      by = py - AY
    const id = sg.getImageData(0, 0, FW, FH),
      data = id.data
    for (let j = 0; j < FH; j++) {
      const wy = by + j
      if (wy < 0 || wy >= H) continue
      for (let k = 0; k < FW; k++) {
        const wx = bx + k
        if (wx < 0 || wx >= W) continue
        if (depth[wy * W + wx] > robotDepth) data[(j * FW + k) * 4 + 3] = 0
      }
    }
    sg.putImageData(id, 0, 0)
    ctx.drawImage(this.scratch, bx, by)
  }
}
