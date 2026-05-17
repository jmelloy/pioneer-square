import { reactive, watch, onUnmounted, type ComputedRef } from 'vue'
import type { Agent, Task } from '../types'
import {
  type AgentPose,
  type Poi,
  buildErrandQueue,
  defaultRng,
  lingerFor,
  personalityFor,
  poisFromGeometry,
} from './useErrands'

export interface Position {
  x: number
  y: number
}

export interface ChoreographyRow {
  index: number
  task: Task | null
  activityKey: string | null
  agentId: string | null
}

export interface ChoreographyOptions {
  agents: ComputedRef<Agent[]>
  taskRows: ComputedRef<ChoreographyRow[]>
  rowHeight: ComputedRef<number>
  tableWidth: ComputedRef<number>
  breakRoomTop: ComputedRef<number>
  breakRoomHeight: ComputedRef<number>
  tableLeft: number
  tableTop: number
  rowGap: number
  stations: ReadonlyArray<{ key: string; frac: number }>
}

export function useAgentChoreography(opts: ChoreographyOptions) {
  const positions = reactive<Record<string, Position>>({})
  const walking = reactive<Record<string, boolean>>({})
  const pose = reactive<Record<string, AgentPose>>({})
  const duration = reactive<Record<string, number>>({})
  const timers: Record<string, ReturnType<typeof setTimeout>> = {}
  const prevTargetKey: Record<string, string> = {}
  // Tracks each robot's current destination for collision avoidance
  const reservedPos: Record<string, Position> = {}
  // Per-agent errand chain; refilled on demand from useErrands.
  const errandQueue: Record<string, Poi[]> = {}
  // Linger duration carried from target selection into the post-arrival timer.
  const pendingLinger: Record<string, number> = {}
  // Pose to apply on arrival at the current target.
  const pendingPose: Record<string, AgentPose> = {}

  function rowYFor(rowIndex: number) {
    return opts.tableTop + rowIndex * (opts.rowHeight.value + opts.rowGap)
  }

  function stationPos(rowIndex: number, key: string): Position {
    const station = opts.stations.find((s) => s.key === key)!
    const x = opts.tableLeft + station.frac * opts.tableWidth.value
    const y = rowYFor(rowIndex) + opts.rowHeight.value * 0.3
    return { x, y }
  }

  function rowCenterPos(rowIndex: number): Position {
    return {
      x: opts.tableLeft + opts.tableWidth.value * 0.5,
      y: rowYFor(rowIndex) + opts.rowHeight.value * 0.3,
    }
  }

  // Distribute idle agents into per-slot lanes so they don't pile up in one corner.
  function randomBreakRoomPos(agentId?: string): Position {
    const xMin = opts.tableLeft + 30
    const xMax = opts.tableLeft + opts.tableWidth.value - 30
    const yMin = opts.breakRoomTop.value + 24
    const yMax = opts.breakRoomTop.value + Math.max(28, opts.breakRoomHeight.value - 16)
    let x: number
    if (agentId !== undefined) {
      const idx = opts.agents.value.findIndex((a) => a.id === agentId)
      const total = Math.max(opts.agents.value.length, 1)
      if (idx >= 0 && total > 1) {
        const lane = (idx + 0.5 + (Math.random() - 0.5) * 0.6) / total
        x = xMin + Math.max(0, Math.min(1, lane)) * (xMax - xMin)
      } else {
        x = xMin + Math.random() * (xMax - xMin)
      }
    } else {
      x = xMin + Math.random() * (xMax - xMin)
    }
    return {
      x,
      y: yMin + Math.random() * Math.max(2, yMax - yMin),
    }
  }

  // Pick a random position near any of the available table rows
  function randomTableWanderPos(): Position {
    const rows = opts.taskRows.value
    if (rows.length === 0) return randomBreakRoomPos()
    const row = rows[Math.floor(Math.random() * rows.length)]
    const xMin = opts.tableLeft + 40
    const xMax = opts.tableLeft + opts.tableWidth.value - 40
    return {
      x: xMin + Math.random() * (xMax - xMin),
      y: rowYFor(row.index) + opts.rowHeight.value * 0.5,
    }
  }

  // Returns true if candidate is within minDist of any other robot's reserved destination
  function isCrowded(candidate: Position, excludeId: string, minDist = 36): boolean {
    return Object.entries(reservedPos).some(
      ([id, pos]) =>
        id !== excludeId && Math.hypot(candidate.x - pos.x, candidate.y - pos.y) < minDist,
    )
  }

  // Pick a wander position spread across all tables, avoiding other robots' destinations
  function pickWanderPos(agentId: string): Position {
    for (let attempt = 0; attempt < 8; attempt++) {
      // 60% chance to wander to a random table row, 40% to the break room
      const candidate = Math.random() < 0.6 ? randomTableWanderPos() : randomBreakRoomPos()
      if (!isCrowded(candidate, agentId)) return candidate
    }
    return randomBreakRoomPos()
  }

  function getPos(id: string): Position {
    return positions[id] || { x: opts.tableLeft + 60, y: opts.tableTop + opts.rowHeight.value }
  }

  // Match by agentId so each robot goes to its own table, not all to the same one.
  function rowForAgent(agent: Agent): ChoreographyRow | null {
    return opts.taskRows.value.find((r) => r.agentId === agent.id) ?? null
  }

  function isWorking(agent: Agent) {
    return !['idle', 'offline'].includes(agent.state)
  }

  // Small golden-angle spiral offset per slot index prevents robots from stacking
  // exactly on top of each other when they share a workbench position.
  function slotJitter(agentId: string): Position {
    const idx = opts.agents.value.findIndex((a) => a.id === agentId)
    if (idx <= 0) return { x: 0, y: 0 }
    const angle = (idx * 137.508 * Math.PI) / 180
    const r = Math.min(idx * 5, 16)
    return { x: Math.cos(angle) * r, y: Math.sin(angle) * r }
  }

  function nextErrand(agentId: string): Poi {
    const geom = {
      tableLeft: opts.tableLeft,
      tableWidth: opts.tableWidth.value,
      breakRoomTop: opts.breakRoomTop.value,
      breakRoomHeight: opts.breakRoomHeight.value,
    }
    const pois = poisFromGeometry(geom)
    // Skip POIs another robot is already reserved on, so the queue surfaces
    // an open seat rather than queueing two robots on the same couch cushion.
    const free = pois.filter((p) => !isCrowded(p.pos, agentId, 24))
    const usable = free.length ? free : pois

    if (!errandQueue[agentId] || errandQueue[agentId].length === 0) {
      const wanderFactory = (): Poi => ({
        kind: 'wander',
        pos: pickWanderPos(agentId),
        lingerMs: [1200, 2600],
        pose: 'stand',
      })
      errandQueue[agentId] = buildErrandQueue(
        personalityFor(agentId),
        usable,
        wanderFactory,
        defaultRng,
      )
    }
    return errandQueue[agentId].shift()!
  }

  function targetForAgent(agent: Agent): Position {
    const row = rowForAgent(agent)
    const jitter = slotJitter(agent.id)
    if (row && isWorking(agent)) {
      const key = row.activityKey
      const base = key ? stationPos(row.index, key) : rowCenterPos(row.index)
      pendingPose[agent.id] = 'stand'
      return { x: base.x + jitter.x, y: base.y + jitter.y }
    }
    const errand = nextErrand(agent.id)
    pendingLinger[agent.id] = lingerFor(errand, defaultRng)
    pendingPose[agent.id] = errand.pose
    return errand.pos
  }

  function targetKey(agent: Agent): string {
    const row = rowForAgent(agent)
    if (row && isWorking(agent)) {
      return `task:${row.task?.id ?? row.index}:${row.activityKey ?? 'center'}`
    }
    return 'idle'
  }

  // Snap to integer pixels so stepped CSS transitions land on the pixel grid.
  function snap(p: Position): Position {
    return { x: Math.round(p.x), y: Math.round(p.y) }
  }

  function moveAgent(id: string, target: Position) {
    const snapped = snap(target)
    reservedPos[id] = snapped
    const cur = positions[id] || snapped
    const dist = Math.hypot(snapped.x - cur.x, snapped.y - cur.y)
    const dur = Math.max(0.6, dist / 120)
    duration[id] = dur
    walking[id] = true
    // Robot is in transit — drop any sit/lean pose until they arrive.
    pose[id] = 'stand'
    positions[id] = snapped
    clearTimeout(timers[id])
    timers[id] = setTimeout(
      () => {
        walking[id] = false
        pose[id] = pendingPose[id] ?? 'stand'
        const agent = opts.agents.value.find((a) => a.id === id)
        if (agent && !isWorking(agent)) scheduleIdleStroll(id)
      },
      dur * 1000 + 60,
    )
  }

  function scheduleIdleStroll(id: string) {
    // Linger picked at the moment the previous target was selected so each
    // POI honors its own dwell time (couch lingers way longer than water).
    const linger = pendingLinger[id] ?? 3500 + Math.random() * 4000
    clearTimeout(timers[id])
    timers[id] = setTimeout(() => {
      const agent = opts.agents.value.find((a) => a.id === id)
      if (!agent || isWorking(agent)) return
      moveAgent(id, targetForAgent(agent))
    }, linger)
  }

  function syncAgent(agent: Agent) {
    const key = targetKey(agent)
    if (prevTargetKey[agent.id] === key && positions[agent.id]) return
    prevTargetKey[agent.id] = key
    if (!positions[agent.id]) {
      positions[agent.id] = snap(pickWanderPos(agent.id))
    }
    moveAgent(agent.id, targetForAgent(agent))
  }

  function syncAll() {
    opts.agents.value.forEach(syncAgent)
  }

  watch(
    () =>
      opts.agents.value
        .map((a) => `${a.id}:${a.state}:${a.workerId}:${a.activity ?? ''}`)
        .join(','),
    syncAll,
  )
  watch(opts.taskRows, syncAll)

  onUnmounted(() => {
    Object.values(timers).forEach(clearTimeout)
  })

  return {
    positions,
    walking,
    pose,
    duration,
    syncAll,
    getPos,
  }
}
