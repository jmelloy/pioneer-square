/** Shared types for the factory-floor scene. No runtime code here. */

export interface TableRect {
  name: string
  /** Footprint edges in tile units (the benches do NOT line up with whole tiles). */
  c0: number
  c1: number
  r0: number
  r1: number
}

export interface DoohickeySpot {
  /** Tile-space position on the bench top where the doohickey sits. */
  pos: [number, number]
  /** Index into doohickeys.json `types` (0 gearbox, 1 steam rig, 2 scope console). */
  type: number
}

export interface Spot {
  /** The "W" tile a robot stands on while working. */
  tile: [number, number]
  doohickey: DoohickeySpot
}

export interface Behavior {
  /** Walking speed, tiles per second. */
  speed: number
  workDwell: [number, number]
  breakDwell: [number, number]
  /** Added to a robot's depth when it works or stands beside a bench edge. */
  depthBonus: number
  besideEdge: number
  besideFarEnd: number
}

export interface SceneConfig {
  image: { width: number; height: number }
  projection: {
    tileWidth: number
    origin: { x: number; y: number }
    lift: number
    thickness: number
  }
  /** One string per row (r). Character index = column (c). See `legend`. */
  grid: string[]
  legend: Record<string, string>
  tables: TableRect[]
  /** Inclusive tile ranges that belong to a bench (they get exact geometry, not box occluders). */
  tableTiles: { c: [number, number]; r: [number, number] }[]
  defaultPropHeight: number
  /** "c,r" -> box height in native px for blocked, non-bench tiles. */
  propHeights: Record<string, number>
  spots: Spot[]
  behavior: Behavior
}

export type Mode = 'work' | 'break'
export type Act = 'coffee' | 'board' | null
/** Facing: 0 SE (+c), 1 SW (+r), 2 NW (-c), 3 NE (-r). Matches the atlas row order. */
export type Yaw = 0 | 1 | 2 | 3

export interface Robot {
  /** Position in tile units; integers are tile centres. */
  c: number
  r: number
  path: [number, number][]
  wait: number
  mode: Mode
  /** Target tile (-1 when none). */
  tc: number
  tr: number
  act: Act
  /** Seconds spent acting or working at the current spot. */
  actT: number
  /** Body type: 0 boiler-bot, 1 lanky bot, 2 cart-bot. */
  type: number
  /** Colourway 0 or 1. */
  cw: number
  seed: number
  yaw: Yaw
  working: boolean
}

export interface RobotAtlasMeta {
  FW: number
  FH: number
  AX: number
  AY: number
  types: string[]
  dirs: string[]
  anims: Record<string, { off: number; n: number }>
  /** anchors[type][dir][frameIndex] = hand tip in frame pixels. */
  anchors: Record<string, Record<string, [number, number][]>>
  shadow: Record<string, [number, number]>
  lamp: { main: [number, number, number]; hi: [number, number, number] }
}

export interface DoohickeyAtlasMeta {
  FW: number
  FH: number
  AX: number
  AY: number
  types: string[]
  /** puff[type][dir] = chimney tip in frame pixels (steam rig only). */
  puff: Record<string, Record<string, [number, number]>>
}
