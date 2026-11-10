/** Per-frame composition: background, then robots and doohickeys sorted by depth. Browser only. */
import type { DoohickeyAtlasMeta, Robot, RobotAtlasMeta, SceneConfig } from './types.ts'
import { makeGeometry } from './scene.ts'
import { buildDepth, robotDepth } from './depth.ts'
import { RobotSprites } from './sprites.ts'

export interface LoadedAssets {
  background: HTMLImageElement
  robots: HTMLImageElement
  doohickeys: HTMLImageElement
  propMask: HTMLImageElement
}

export function loadImage(url: string): Promise<HTMLImageElement> {
  return new Promise((res, rej) => {
    const im = new Image()
    im.onload = () => res(im)
    im.onerror = () => rej(new Error('failed to load ' + url))
    im.src = url
  })
}

function readMask(img: HTMLImageElement, W: number, H: number): Uint8Array {
  const c = document.createElement('canvas')
  c.width = W
  c.height = H
  const g = c.getContext('2d', { willReadFrequently: true })!
  g.drawImage(img, 0, 0)
  const d = g.getImageData(0, 0, W, H).data,
    m = new Uint8Array(W * H)
  for (let i = 0; i < W * H; i++) m[i] = d[i * 4] > 127 ? 1 : 0
  return m
}

const ZONE_COLORS: Record<string, string> = {
  '-': 'rgba(60,200,120,.18)',
  '#': 'rgba(235,70,70,.4)',
  W: 'rgba(70,130,255,.5)',
  C: 'rgba(255,150,40,.55)',
  B: 'rgba(250,215,60,.55)',
}

export class FactoryRenderer {
  private ctx: CanvasRenderingContext2D
  private geo
  private depth: Int16Array
  private sprites: RobotSprites
  private W: number
  private H: number
  private scene: SceneConfig
  private assets: LoadedAssets
  private dooMeta: DoohickeyAtlasMeta
  private robotMeta: RobotAtlasMeta
  private spotByTile = new Map<string, SceneConfig['spots'][number]>()

  constructor(
    canvas: HTMLCanvasElement,
    scene: SceneConfig,
    assets: LoadedAssets,
    robotMeta: RobotAtlasMeta,
    dooMeta: DoohickeyAtlasMeta,
  ) {
    this.scene = scene
    this.assets = assets
    this.robotMeta = robotMeta
    this.dooMeta = dooMeta
    this.W = scene.image.width
    this.H = scene.image.height
    canvas.width = this.W
    canvas.height = this.H
    this.ctx = canvas.getContext('2d')!
    this.geo = makeGeometry(scene)
    this.depth = buildDepth(scene, readMask(assets.propMask, this.W, this.H))
    this.sprites = new RobotSprites(assets.robots, robotMeta)
    for (const s of scene.spots) this.spotByTile.set(s.tile.join(','), s)
  }

  /** Draw one frame. `tm` is seconds (any monotonic clock). */
  render(tm: number, robots: Robot[], opts: { showZones?: boolean } = {}) {
    const { ctx, scene, geo } = this
    ctx.imageSmoothingEnabled = false
    ctx.drawImage(this.assets.background, 0, 0)

    type Item = {
      d: number
      kind: 'robot' | 'doo'
      b: Robot
      i: number
      spot?: SceneConfig['spots'][number]
    }
    const items: Item[] = robots.map((b, i) => ({ d: b.c + b.r, kind: 'robot' as const, b, i }))
    robots.forEach((b, i) => {
      const spot = b.working && b.actT > 0.25 ? this.spotByTile.get(b.tc + ',' + b.tr) : undefined
      if (spot)
        items.push({ d: spot.doohickey.pos[0] + spot.doohickey.pos[1], kind: 'doo', b, i, spot })
    })
    // doohickeys sort before robots at equal depth
    items.sort((a, b) => a.d - b.d || (a.kind === 'doo' ? -1 : 1) - (b.kind === 'doo' ? -1 : 1))

    for (const it of items) {
      if (it.kind === 'robot') {
        const [x, y] = geo.tileCentre(it.b.c, it.b.r)
        this.sprites.draw(
          ctx,
          it.b,
          it.i,
          tm,
          Math.round(x),
          Math.round(y),
          this.depth,
          this.W,
          this.H,
          robotDepth(scene, it.b),
        )
      } else {
        this.drawDoohickey(it.b, it.spot!, tm)
      }
    }
    if (opts.showZones) this.drawZones()
  }

  private drawDoohickey(b: Robot, spot: SceneConfig['spots'][number], tm: number) {
    const { ctx, dooMeta: m } = this
    const [pc, pr] = spot.doohickey.pos,
      tp = spot.doohickey.type
    const dir = (b.yaw + 2) % 4,
      f = Math.floor(tm * 6 + b.seed * 5) % 4 // it faces the robot
    const [bx, by] = this.geo.benchTop(pc, pr)
    const x = Math.round(bx),
      y = Math.round(by)
    ctx.drawImage(
      this.assets.doohickeys,
      f * m.FW,
      (tp * 4 + dir) * m.FH,
      m.FW,
      m.FH,
      x - m.AX,
      y - m.AY,
      m.FW,
      m.FH,
    )
    if (tp === 1) {
      // steam rig puffs
      const a = m.puff[1][this.robotMeta.dirs[dir]]
      for (let k = 0; k < 3; k++) {
        const ph = (tm * 1.8 + k / 3 + b.seed) % 1
        ctx.fillStyle = `rgba(222,218,165,${(0.65 * (1 - ph)).toFixed(2)})`
        ctx.fillRect(
          x - m.AX + a[0] + Math.round(Math.sin((tm + k) * 3)),
          y - m.AY + a[1] - Math.round(ph * 9) - 1,
          2,
          2,
        )
      }
    }
  }

  /** Debug overlay: the walk grid in iso tiles. */
  private drawZones() {
    const { ctx, scene, geo } = this
    const hw = scene.projection.tileWidth / 2,
      hh = scene.projection.tileWidth / 4,
      { x: ox, y: oy } = scene.projection.origin
    ctx.lineWidth = 1
    for (let r = 0; r < geo.ROWS; r++)
      for (let c = 0; c < geo.COLS; c++) {
        const k = geo.kind(c, r)
        if (k === '.') continue
        const tx = ox + (c - r) * hw,
          ty = oy + (c + r) * hh
        ctx.beginPath()
        ctx.moveTo(tx, ty)
        ctx.lineTo(tx + hw, ty + hh)
        ctx.lineTo(tx, ty + 2 * hh)
        ctx.lineTo(tx - hw, ty + hh)
        ctx.closePath()
        ctx.fillStyle = ZONE_COLORS[k]
        ctx.fill()
        ctx.strokeStyle = 'rgba(255,255,255,.3)'
        ctx.stroke()
      }
  }
}
