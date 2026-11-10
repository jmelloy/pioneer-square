# Factory-floor renderer

## Source

- `HANDOFF.md`: supplied standalone handoff, preserved verbatim.
- `files-2.zip`: supplied archive, preserved verbatim. It contains a nested
  `factory-floor-handoff.zip` with the self-contained reference, generators,
  original art, demo component, simulation, and tests.
- `live-desktop.png` / `live-mobile.png`: initial whole-room Chromium screenshots.
- `zoomed-work-area.png`: work-area zoom before the 80% threshold adjustment,
  at a 1040×1060 viewport, close
  to the center panel in the supplied desktop screenshot.
  All use real Pinia stores populated with fixture agents/tasks; these are
  renderer checks, not screenshots of a connected backend.

The production assets, scene geometry, depth buffer, and sprite renderer live in
`frontend/src/components/factory-floor/`. Geometry and art are unchanged from the
handoff (text files are formatted to repository conventions).

## Live integration

`frontend/src/components/FactoryFloor.vue` still mounts through the existing
`MainView.vue` factory tab. It uses the existing agents, tasks, and UI stores;
there is no new network protocol, dependency, or demo route.

`factory-floor/state.ts` replaces the demo's random work/break decisions:

- Each online agent gets a robot keyed by its agent ID. New robots choose a
  least-used body type among online robots, with the ID hash breaking ties;
  colourways remain ID-hashed. Existing robots keep their appearance during
  updates. Body assignments can change after a remount or reconnection, as the
  current mix determines availability. Offline/removed agents disappear; an empty
  guild has no robots.
- `working`, `thinking`, and `busy` agents with a linked non-terminal task go
  to a bench and work until their real agent/task state changes. The link is
  `agent.taskId`, never just the shared `workerId`.
- Idle/error agents, agents without a resolved task, and agents whose task is
  done/failed/cancelled visit coffee/the board. This wandering is presentation
  only; it never changes stores or fabricates work.
- Bench destinations are exclusive. There is no eight-agent cap; overflow
  agents wait on open floor until a bench is available. Only a robot actually
  working at a bench produces a widget.
- `sim.ts` supplies the original BFS and bench-facing helpers. Its randomized
  `spawn()` and `step()` are retained for the original tests, but never invoked
  by the live component.
- Agent buttons retain keyboard access, names, state/activity/task tooltips,
  and `uiStore.selectAgent()` behavior. The ticker uses real store data.
- The scene selects the largest integer zoom for which at least 80% fits:
  `max(1, floor(min(viewportWidth / 297, viewportHeight / 383) / 0.8))`.
  For example, 2× becomes 3× at fit scale 2.4, not 2.0. On resize, the
  viewport scrolls toward the mean work-spot position, clamped to the room edges.
  Walls/edge machinery may be cropped; scrolling and keyboard focus can reveal
  them. Geometry stays at native resolution, and agent targets share the zoom.
  This intentionally replaces both whole-room integer fit and fractional fit.
  Current Chromium checks cover desktop/mobile cropping, agent selection, and
  keyboard focus revealing an offscreen agent without console errors.
- Reduced motion snaps robots to their real destinations and freezes all
  animation clocks (including widgets/steam and ticker). Explicit `paused`
  freezes in place; `paused=false` overrides the motion preference.
- Asset-load/render-initialization errors are surfaced; unmount cancels RAF,
  the resize observer, and the media-query listener, including during loading.

## Verification

From `frontend/`:

```sh
npm test
npm run build
npx eslint src/components/FactoryFloor.vue src/components/__tests__/FactoryFloor.spec.ts src/components/factory-floor
```

The original 16 tests are integrated under `factory-floor/__tests__/` with their
assertions preserved; only imports, JSON loading, and formatting were adapted
for Vitest. Live-state tests cover assignment, terminal tasks, idle/error,
identity preservation, overflow, removal, and safe mid-walk rerouting. Component
tests cover store updates, selection, scaling, pause/reduced motion, load errors,
and unmount/late-load cleanup.

Also checked manually using headless Chromium: actual canvas drawing and assets,
eight fixture agents and six working bench widgets, desktop/mobile sizes,
selection, store state changes, motion-preference changes, and frozen reduced-
motion canvas pixels. No browser console errors. The self-contained reference
was opened for visual comparison. Full backend/WebSocket integration and an
exhaustive pixel-by-pixel comparison of all poses were not run.
