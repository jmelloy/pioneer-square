import type { Agent, Task } from '../../types'
import type { Robot, SceneConfig, Yaw } from './types'
import { Sim } from './sim'

export interface AgentRobot extends Robot {
  agentId: string
  taskId: string | null
}

function identity(id: string) {
  let hash = 0
  for (const char of id) hash = (Math.imul(hash, 31) + char.charCodeAt(0)) >>> 0
  return hash
}

/** Live state owns work/break; only idle wandering is cosmetic. Never runs Sim.step(). */
export class FactoryState {
  robots: AgentRobot[] = []
  readonly paths: Sim

  constructor(private scene: SceneConfig) {
    this.paths = new Sim(scene)
  }

  sync(agents: Pick<Agent, 'id' | 'state' | 'taskId'>[], tasks: Pick<Task, 'id' | 'state'>[]) {
    const online = agents.filter((a) => a.state !== 'offline')
    const ids = new Set(online.map((a) => a.id))
    this.robots = this.robots.filter((b) => ids.has(b.agentId))
    const taskById = new Map(tasks.map((task) => [task.id, task]))
    const floor = this.paths.geo.listTiles('-')
    const typeCounts = [0, 0, 0]
    for (const robot of this.robots) typeCounts[robot.type]++

    for (const agent of online) {
      const task = agent.taskId ? taskById.get(agent.taskId) : undefined
      const atWork =
        ['working', 'thinking', 'busy'].includes(agent.state) &&
        task &&
        !['done', 'failed', 'cancelled'].includes(task.state)
      const mode = atWork ? 'work' : 'break'
      let b = this.robots.find((robot) => robot.agentId === agent.id)
      if (!b) {
        const hash = identity(agent.id)
        const leastUsed = [0, 1, 2].filter((type) => typeCounts[type] === Math.min(...typeCounts))
        const type = leastUsed[hash % leastUsed.length]
        typeCounts[type]++
        const [c, r] = floor[hash % floor.length]
        b = {
          agentId: agent.id,
          taskId: null,
          c,
          r,
          path: [],
          wait: 0,
          mode,
          tc: -1,
          tr: -1,
          act: null,
          actT: 0,
          type,
          cw: Math.floor(hash / 3) % 2,
          seed: (hash % 1000) / 1000,
          yaw: 1,
          working: false,
        }
        this.robots.push(b)
      }
      if (b.mode !== mode || b.taskId !== (task?.id ?? null)) {
        b.mode = mode
        b.taskId = task?.id ?? null
        b.tc = -1
        b.tr = -1
        b.working = false
        b.act = null
        b.actT = 0
      }
    }
    // Release old destinations before allocating, so simultaneous state changes cannot steal seats.
    for (const b of this.robots) {
      if (b.tc < 0 || (b.mode === 'work' && this.paths.geo.kind(b.tc, b.tr) !== 'W')) {
        this.chooseTarget(b)
      }
    }
  }

  private chooseTarget(b: AgentRobot) {
    const taken = new Set(
      this.robots
        .filter((other) => other !== b && other.tc >= 0)
        .map((other) => `${other.tc},${other.tr}`),
    )
    const free = (chars: string) =>
      this.paths.geo
        .listTiles(chars)
        .filter(([c, r]) => !taken.has(`${c},${r}`) && (c !== b.tc || r !== b.tr))
    const previous = b.tc >= 0 ? this.paths.geo.kind(b.tc, b.tr) : ''
    const zones = b.mode === 'work' ? 'W' : previous === 'C' ? 'B' : previous === 'B' ? 'C' : 'CB'
    const spots = free(zones)
    // More live agents than seats: wait on open floor, never invent another occupied bench.
    const pool = spots.length ? spots : free('-')
    if (!pool.length) return
    const target = pool[(identity(b.agentId) + Math.floor(b.seed * 10)) % pool.length]
    // Finish the current grid edge before rerouting, rather than cutting diagonally through props.
    const next = b.path[0]
    const start = next ?? [Math.round(b.c), Math.round(b.r)]
    const path = this.paths.findPath(start[0], start[1], target[0], target[1])
    if (!path) return
    b.path = next ? [next, ...path] : path
    ;[b.tc, b.tr] = target
    b.act = null
    b.working = false
    b.actT = 0
    if (!b.path.length) this.arrive(b)
  }

  private arrive(b: AgentRobot) {
    const kind = this.paths.geo.kind(b.tc, b.tr)
    b.working = b.mode === 'work' && kind === 'W'
    b.act = b.mode === 'break' ? (kind === 'C' ? 'coffee' : kind === 'B' ? 'board' : null) : null
    b.yaw = b.working ? this.paths.yawToTable(b.tc, b.tr) : b.act ? 3 : b.yaw
    b.actT = 0
    b.wait = 6 + b.seed * 4
  }

  settle() {
    for (const b of this.robots) {
      if (b.tc < 0 || (!b.path.length && (b.working || b.act))) continue
      b.c = b.tc
      b.r = b.tr
      b.path = []
      this.arrive(b)
      // Reduced-motion still shows the widget belonging to a working agent.
      if (b.working) b.actT = 0.3
    }
  }

  step(dt: number) {
    for (const b of this.robots) {
      if (b.path.length) {
        const [c, r] = b.path[0]
        const dx = c - b.c,
          dy = r - b.r
        const distance = Math.hypot(dx, dy)
        const move = this.scene.behavior.speed * dt
        b.yaw = (Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? 0 : 2) : dy > 0 ? 1 : 3) as Yaw
        if (distance <= move) {
          b.c = c
          b.r = r
          b.path.shift()
          if (!b.path.length) this.arrive(b)
        } else {
          b.c += (dx / distance) * move
          b.r += (dy / distance) * move
        }
      } else {
        b.actT += dt
        if (b.mode === 'break') {
          b.wait -= dt
          if (b.wait <= 0) this.chooseTarget(b)
        }
      }
    }
  }
}
