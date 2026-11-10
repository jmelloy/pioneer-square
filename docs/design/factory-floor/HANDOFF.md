# Factory floor: handoff to implement as a Vue component

An isometric pixel-art factory room with wandering robots. Robots walk between table work
spots (working), a coffee machine and a task board (on break). They face the way they walk,
pass behind the tables and boilers, and run per-state animations.

A working, single-file reference exists (`reference/factory-scene.html`). It is the source of
truth for look and behaviour. Your job is a Vue 3 + TypeScript component that matches it.

![reference scene](docs/scene.png)

## What you are being handed

| Path | What it is | Status |
|---|---|---|
| `reference/factory-scene.html` | Complete working demo, self-contained (assets base64-embedded). Open in a browser. Has a dev "Edit prop heights" mode. | Verified in headless Chromium |
| `src/factory-floor/scene.json` | **Canonical scene data**: grid, bench geometry, spots, prop heights, behaviour constants | Tested |
| `src/factory-floor/types.ts`, `scene.ts` | Types and geometry helpers (tile ↔ screen) | Tested |
| `src/factory-floor/sim.ts` | Pathing + robot state machine. Pure, seedable | Tested (16 tests) |
| `src/factory-floor/depth.ts` | Occlusion depth buffer + robot depth rule. Pure | Tested |
| `src/factory-floor/sprites.ts` | Atlas variants, frame picking, per-pixel depth-masked draw | **Not executed** (needs a DOM) |
| `src/factory-floor/renderer.ts` | Per-frame composition | **Not executed** |
| `src/factory-floor/FactoryFloor.vue` | The component | **Not executed**; script block parses |
| `src/factory-floor/assets/` | Background, prop mask, robot atlas + json, doohickey atlas + json | Reproducible from `tools/` |
| `tools/sprites/` | Python (numpy + Pillow) generator for both atlases | Reproduces shipped PNGs byte-for-byte |
| `tools/make_prop_mask.py` | Builds `prop-mask.png` from the background | Reproduces shipped file |
| `reference/pixel-snap.html` | The tool that turned the AI-generated source image into true pixel art (grid detect + palette reduce) | Used once |
| `tools/source/factory-floor-original.jpg` | The original generated image (896×1152), for provenance | |

The non-DOM modules were written to be runnable and testable under plain Node. The DOM layer
(`sprites.ts`, `renderer.ts`, the SFC) was ported line-for-line from the reference page but could
not be run where it was authored. Treat it as a faithful first draft: run it, compare against the
reference, fix what differs.

## Do this, in order

1. Copy `src/factory-floor/` into the app (for example `src/components/factory-floor/`).
2. Run the logic tests: `npm test` in this package. Node ≥ 22.6 (uses `--experimental-strip-types`).
   Modules import each other with explicit `.ts` extensions and use no TS-only runtime syntax
   (no parameter properties, no enums). In the app set `allowImportingTsExtensions` (Vite is
   fine with it) or rewrite the imports. Also needed: `resolveJsonModule` for the `.json` imports.
3. Mount `<FactoryFloor />` on a scratch page. Open `reference/factory-scene.html` next to it and
   compare using the checklist at the end.
4. Fix any DOM-layer defects. Keep the pure modules pure: the tests are what protect the logic.
5. Add component tests (Vitest + `@vitest/web-worker` or `canvas` mock) for mounting,
   `paused`, and `robots` prop changes; keep the unit tests as they are.
6. Do not change the art, the grid, or the heights without reading "Changing things" below.

## Coordinate system (read this first; everything depends on it)

The scene is drawn at the background's **native size, 297×383 px**, then scaled by an
**integer** factor with `image-rendering: pixelated`. Never scale fractionally: it breaks the
pixel grid. All coordinates below are native pixels.

- **Tiles.** Tile `(c, r)` lives in a 14×14 grid. `c` increases toward screen **down-right**,
  `r` toward screen **down-left**. A floor tile is a 32×16 diamond.
- **Tile centre on screen** (this is where a robot's feet go):
  `x = ox + (c − r)·tw/2`, `y = oy + (c + r + 1)·tw/4`, with `tw = 32`, origin `(ox, oy) = (157, 167)`.
  Fractional `c`, `r` interpolate, so a walking robot just uses its float position.
- **Raised points.** Anything `h` px above the floor is drawn `h` px higher (`y − h`).
  Bench tops are `lift = 15` px up; bench slab thickness is 6 px.
- **Facing (yaw)** matches the atlas row order: `0` SE (moving +c), `1` SW (+r), `2` NW (−c), `3` NE (−r).
- **Depth** of a thing standing on tile `(c, r)` is `c + r`. Bigger = closer to the viewer.

`scene.ts` has the helpers (`tileCentre`, `benchTop`, `screenToTile`). Use them; do not re-derive.

## The grid (`scene.json → grid`)

14 strings, one per row `r`; character index is column `c`.

```
-B-B-CC--.....
----------....
--##--##---...
-W##--##---#..
--##--##W--###
--##--##--####
-W##--##--####
--##--##W--###
--##--##---###
------#-----##
.--W---W----##
.##---------##
...###-----###
....#########.
```

`.` outside floor · `-` walkable · `#` blocked prop · `W` table work spot · `C` coffee spot · `B` task-board spot
(`W`, `C`, `B` are walkable). Walking is 4-connected. The tests assert the walkable region is one
connected component and every spot is reachable from everywhere.

The two benches do **not** line up with whole tiles. Their exact footprints are in `scene.json → tables`
(tile-space rectangles). The `#` tiles inside `tableTiles` ranges are legacy painting; benches are
occluded by their exact rectangles, not by those tiles.

## Behaviour spec (what `sim.ts` implements; do not reinvent)

**Spawn.** `n` robots (1–8), each on a random walkable tile. Robot `i`: body type `i % 3`
(0 boiler-bot, 1 lanky, 2 cart), colourway `floor(i/3) % 2`, starting mode alternating work/break,
`seed = i·0.37` (desynchronises animation phase), initial wait 0–2 s.

**Loop.** When a robot's wait expires it flips mode (only if it had reached its target), picks a
destination, walks there by BFS at 1.8 tiles/s, and on arrival waits:

- **work** → a free `W` tile; dwell 8–16 s. On arrival `working = true`, and it turns to face the nearest bench
  (scan 4 directions up to 4 tiles for a bench tile).
- **break** → a `C` or `B` tile; dwell 6–10 s. On arrival `act = 'coffee'` or `'board'`, yaw forced to 3 (NE).
- Two robots never take the same target while a free one exists.
- While walking, yaw follows the dominant axis of the next step.

**Frame selection** (`sprites.ts → pickFrame`):

| State | Anim | Frames | Timing |
|---|---|---|---|
| walking | `walk` | 4 | 8 fps, phase offset by `seed` |
| idle | `idle` | 2 | 1.5 fps |
| working at a bench | `work` | 4 | 4 fps |
| coffee | `coffee` | 4 | loop 3.2 s: frame 0 until 0.8 s, 1 until 1.8, 2 until 2.8, then 3 |
| board | `board` | 4 | loop 3.4 s: frame 0 until 0.9 s, then tap alternating 1/2 at 5 Hz until 2.0, frame 3 until 2.9, then 0 |

`actT` counts seconds since arrival (for both acting and working).

**Overlays**, drawn into the sprite layer at the hand anchor (`atlas.json → anchors`):
coffee frames 1–3: cup with steam; board frame 3: sticky note (colour by `i % 3`);
work frames 1 and 3 (the strikes): a spark.

**Lamps.** Lamp pixels are baked as marker colours (255,0,255) and (255,128,255) and recoloured at load:
working = palette teal, break = palette amber, and a bright "busy" colour **on every work strike frame**
(frames 1 and 3). Robots do not blink (the old procedural version did; not ported).

**Doohickeys.** While `working` and `actT > 0.25`, a small machine appears on the bench in front of
the robot, at `scene.spots[].doohickey.pos` (tile-space, drawn at lift 15), type 0 gearbox / 1 steam rig /
2 scope console. It faces the robot (`(yaw + 2) % 4`), animates 4 frames at 6 fps, and the steam rig
emits three rising pixels from `puff[1][dir]`. It disappears when the robot leaves.

## Rendering pipeline (per frame)

1. Draw `background.png`.
2. Build a list of robots and visible doohickeys. Sort by depth (`c + r`; a doohickey uses its bench
   position). Doohickeys sort before robots at equal depth.
3. Draw each item in order. A robot is drawn to a 64×96 scratch canvas (shadow, sprite, overlays), then
   every pixel whose prop depth `> robotDepth` gets alpha 0, then it is composited. This per-pixel depth
   test is what makes robots pass behind tables and boilers with crisp edges.
4. Optional debug overlay of the grid (`showZones`).

`robotDepth = c + r + 0.01`, plus `scene.behavior.depthBonus` (1.1) when the robot is working **or
beside a bench's left edge or far end**. Without that bonus, bench pixels hide parts of a robot that is
physically above the tabletop. See "Occlusion" below.

## Occlusion (`depth.ts`)

An `Int16Array(W·H)` initialised to −100 ("no prop"). Each prop pixel stores the depth of the tile under it.

- **Benches** use exact geometry: the top face is the rectangle in `scene.tables`, projected at lift 15,
  plus a 6 px slab below it. A bench pixel takes the depth of the footprint tile it sits over (clamped
  into the footprint, so the slab's front faces map to the edge tile).
- **Every other `#` tile** is a box of height `propHeights["c,r"]` (default 26) **intersected with
  `prop-mask.png`**, the places where prop pixels really are in the background. The mask is what stops a
  tall box from clipping robots standing on empty floor beside a round boiler.
- Overlapping boxes keep the **maximum** depth. That is intentional and tested.

Known approximations (all deliberate): occlusion is per-tile depth, not true 3D; a prop's box is a hint
and the mask supplies the real silhouette; the "beside a bench" bonus is a heuristic. Under the tables, the
space between the legs is treated as open for robots behind them only through the slab thickness (legs are not modelled).

## Data formats

**`robots-atlas.png`** is 1152×2304: 18 frame columns × 64 px, 24 rows × 96 px.
Rows: `(type·2 + colourway)·4 + yaw`. Columns: `anims[anim].off + frame`
(`idle` 0–1, `walk` 2–5, `coffee` 6–9, `board` 10–13, `work` 14–17).
**`robots-atlas.json`**: `FW, FH, AX, AY` (frame size and the foot anchor inside a frame), `types`, `dirs`
(`SE, SW, NW, NE`), `anims`, `anchors[type][dir][frameIndex]` (hand tip, for overlays), `shadow[type]`, `lamp`.

**`doohickeys.png`** is 224×624: 4 frames × 56 px, 12 rows × 52 px; rows `type·4 + dir`.
**`doohickeys.json`**: `FW, FH, AX, AY` (origin = base centre on the bench), `types`, `puff[1][dir]`.

**Palette.** Every sprite colour comes from the room's own 24-colour palette, with only the marker lamp
colours added then recoloured to palette teal/amber/yellow. Keep it that way if you add art.

## Component API (`FactoryFloor.vue`)

```vue
<FactoryFloor :robots="4" :paused="false" :show-zones="false" scale="fit" :max-scale="4" :seed="1"
              @ready="..." @stats="s => ({ working: s.working, onBreak: s.onBreak })" />
```

| Prop | Default | Notes |
|---|---|---|
| `robots` | 4 | 1–8; changing it respawns |
| `paused` | `undefined` | `undefined` → paused if `prefers-reduced-motion` |
| `showZones` | false | debug overlay |
| `scale` | `'fit'` | integer, or `'fit'` = floor(container width / 297), capped by `maxScale` |
| `seed` | `undefined` | deterministic simulation for tests / screenshots |
| `label` | description | canvas `aria-label` |

Emits `ready`, and `stats` about twice a second. Exposes `respawn()`.
Requirements: SSR-safe (no DOM at import; everything starts in `onMounted`), cancels its `requestAnimationFrame`
and `ResizeObserver` on unmount, no global state.

Open questions for the person you are working for (not decided here): which page mounts it, whether
the robot count should be user-controllable, whether `stats` should drive anything.

## Changing things

- **Prop heights.** Open `reference/factory-scene.html`, tick *Edit prop heights*, drag over blocked
  tiles with a brush height, watch the teal outline against the real prop top, then *Copy heights* and
  paste into `scene.json → propHeights`. Bench tiles are fixed-shape and ignored.
- **Grid / spots.** Edit `scene.json`, keep `spots` in step with the `W` tiles (the tests enforce it),
  add a doohickey position on the bench for any new `W`, and re-run `npm test`.
- **Robot art.** `tools/sprites/robots.py` (3D models, palette ramps, poses, `SCALE`), then
  `python3 build_atlas.py` (→ `atlas.png`, `atlas.json`), copy over as `robots-atlas.*`.
  **Doohickeys:** `doohickeys.py` (→ `doo.png`, `doo.json`) → `doohickeys.*`. Needs `numpy`, `Pillow`.
  The renderer is a small software ray-caster in the room's exact 2:1 projection (1 unit along X/Y = 1 px right,
  0.5 px down; 1 unit of Z = 1.2247 px up), toon-shaded to 4-step palette ramps with selective outlines.
- **Background.** If `background.png` changes, redo `prop-mask.png` (`tools/make_prop_mask.py`), re-check
  every `propHeights` entry, and re-fit the bench rectangles (corner method in the reference history: four
  measured top-face corners converted to tile space at lift 15).

## Acceptance checklist

Compare against `reference/factory-scene.html` at the same integer scale.

- [ ] `npm test` passes (16 tests); no test was weakened.
- [ ] Component mounts, draws the room, and robots move; no console errors; no work while unmounted.
- [ ] Canvas is crisp (integer scale, pixelated), correct size at scales 1–4, and resizes with its container.
- [ ] Robots face the way they walk (all four directions) and keep feet on tile centres.
- [ ] At a table spot a robot faces the bench, hammers with alternating arms, sparks on strikes, lamps flash bright on strikes; a doohickey appears 0.25 s in and faces the robot.
- [ ] At the coffee spots: reach, pour with a steaming cup, sip, lower. At the board spots: arm up, tapping, then a note.
- [ ] Robots walk behind the benches and the right-hand boilers with clean edges, and are **not** clipped at the bench's left edge (column 1 and column 5) or near the lower-left boilers.
- [ ] `showZones` shows the grid aligned to the floor.
- [ ] `paused` freezes motion but keeps drawing; `prefers-reduced-motion` starts paused.
- [ ] `robots` prop changes respawn; `seed` makes the simulation repeatable.
- [ ] Unmount cancels animation frames and observers.

## Verification status (honest)

Verified here: the logic modules under Node (grid connectivity, reachability, spot ↔ bench consistency, sim
invariants, depth-buffer values, depth bonus); the sprite and doohickey generators reproduce the shipped PNGs
exactly; the reference page ran in headless Chromium with no script errors across many placements (all work
spots, coffee/board poses, walking in four directions, around the benches and boilers).

Not verified here: the TypeScript DOM layer and the SFC (no Vue and no DOM in the authoring sandbox). The
SFC's script block was parse-checked and evaluated against stubs.

## Reference map (reference page → module)

| Reference function | Port |
|---|---|
| `path`, `pick`, `arrive`, `step`, `spawn`, `yawToTable`, `dwell` | `Sim` in `sim.ts` |
| `hexPoly`, `raster`, `buildDepth`, `besideTable` | `depth.ts` (`buildDepth`, `robotDepth`) |
| `buildVariants`, `pickFrame`, `overlays`, `robot` | `RobotSprites` in `sprites.ts` |
| `render`, `doohickey`, `overlay` | `FactoryRenderer` in `renderer.ts` |
| `frame`, `fit`, load logic | `FactoryFloor.vue` |
| Edit-heights tool (`paintAt`, `hoverHex`) | **Not ported**: dev tool, stays in the reference page |

## Known gaps / ideas

- No blink; lamps only change colour.
- `getImageData` per robot per frame is fine for ≤ 8 robots; for many more, precompute per-frame alpha masks or move compositing to WebGL.
- Integer scaling can still blur on fractional device-pixel-ratio screens; if that matters, pick a scale so `scale × devicePixelRatio` is an integer.
- Robots never interact with each other or react to the viewer; both are easy extensions on top of `Sim`.
- The height editor is dev-only and not exposed by the component.
