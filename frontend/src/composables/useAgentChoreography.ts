import { reactive, watch, onUnmounted, type ComputedRef } from 'vue'
import type { Agent, Task } from '../types'

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
  const duration = reactive<Record<string, number>>({})
  const timers: Record<string, ReturnType<typeof setTimeout>> = {}
  const prevTargetKey: Record<string, string> = {}
  // Tracks each robot's current destination for collision avoidance
  const reservedPos: Record<string, Position> = {}

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

  function targetForAgent(agent: Agent): Position {
    const row = rowForAgent(agent)
    const jitter = slotJitter(agent.id)
    if (row && isWorking(agent)) {
      const key = row.activityKey
      const base = key ? stationPos(row.index, key) : rowCenterPos(row.index)
      return { x: base.x + jitter.x, y: base.y + jitter.y }
    }
    return pickWanderPos(agent.id)
  }

  function targetKey(agent: Agent): string {
    const row = rowForAgent(agent)
    if (row && isWorking(agent)) {
      return `task:${row.task?.id ?? row.index}:${row.activityKey ?? 'center'}`
    }
    return 'idle'
  }

  function moveAgent(id: string, target: Position) {
    reservedPos[id] = target
    const cur = positions[id] || target
    const dist = Math.hypot(target.x - cur.x, target.y - cur.y)
    const dur = Math.max(0.6, dist / 120)
    duration[id] = dur
    walking[id] = true
    positions[id] = target
    clearTimeout(timers[id])
    timers[id] = setTimeout(
      () => {
        walking[id] = false
        const agent = opts.agents.value.find((a) => a.id === id)
        if (agent && !isWorking(agent)) scheduleIdleStroll(id)
      },
      dur * 1000 + 60,
    )
  }

  function scheduleIdleStroll(id: string) {
    const linger = 3500 + Math.random() * 4000
    clearTimeout(timers[id])
    timers[id] = setTimeout(() => {
      const agent = opts.agents.value.find((a) => a.id === id)
      if (!agent || isWorking(agent)) return
      moveAgent(id, pickWanderPos(id))
    }, linger)
  }

  function syncAgent(agent: Agent) {
    const key = targetKey(agent)
    if (prevTargetKey[agent.id] === key && positions[agent.id]) return
    prevTargetKey[agent.id] = key
    if (!positions[agent.id]) {
      positions[agent.id] = pickWanderPos(agent.id)
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
    duration,
    syncAll,
    getPos,
  }
}
