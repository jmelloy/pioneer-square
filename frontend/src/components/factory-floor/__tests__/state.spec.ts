import { describe, expect, it } from 'vitest'
import { FactoryState } from '../state'
import { pickFrame } from '../sprites'
import sceneJson from '../scene.json'
import type { SceneConfig } from '../types'
import type { Agent, Task } from '../../../types'

const scene = sceneJson as unknown as SceneConfig
const task: Task = { id: 't-1', state: 'working', worker_id: 'w-1' }
const worker: Agent = {
  id: 'a-1',
  name: 'Worker',
  state: 'working',
  taskId: task.id,
  type: 'worker',
  workerId: 'w-1',
  logs: [],
  joinedAt: '',
}
const advance = (floor: FactoryState, seconds: number) => {
  for (let i = 0; i < seconds * 30; i++) floor.step(1 / 30)
}

describe('live factory state', () => {
  it('only creates robots for real online agents, without an eight-agent cap', () => {
    const floor = new FactoryState(scene)
    floor.sync([], [])
    expect(floor.robots).toEqual([])
    floor.sync(
      Array.from({ length: 12 }, (_, i) => ({ ...worker, id: `a-${i}` })),
      [task],
    )
    expect(floor.robots).toHaveLength(12)
    floor.sync([{ ...worker, state: 'offline' }], [task])
    expect(floor.robots).toEqual([])
  })

  it('balances body types even when agent hashes would choose the same type', () => {
    const floor = new FactoryState(scene)
    const agents = ['a-0', 'a-3', 'a-6', 'a-9'].map((id) => ({ ...worker, id }))
    floor.sync(agents, [task])
    const counts = [0, 1, 2].map((type) => floor.robots.filter((b) => b.type === type).length)
    expect(counts.sort()).toEqual([1, 1, 2])
    const repeat = new FactoryState(scene)
    repeat.sync(agents, [task])
    expect(repeat.robots.map((b) => [b.type, b.cw])).toEqual(
      floor.robots.map((b) => [b.type, b.cw]),
    )
  })

  it('assigns the expanded colourways deterministically from agent IDs', () => {
    const floor = new FactoryState(scene)
    const agents = Array.from({ length: 48 }, (_, i) => ({ ...worker, id: `palette-${i}` }))
    floor.sync(agents, [task])
    expect(new Set(floor.robots.map((b) => b.cw)).size).toBe(8)
    const before = floor.robots.map((b) => [b.agentId, b.cw])
    floor.sync(
      agents.map((agent) => ({ ...agent, state: 'idle' })),
      [task],
    )
    expect(floor.robots.map((b) => [b.agentId, b.cw])).toEqual(before)
  })

  it('fills the least-used type on join without changing existing appearances', () => {
    const floor = new FactoryState(scene)
    const agents = ['a-0', 'a-3', 'a-6'].map((id) => ({ ...worker, id }))
    floor.sync(agents, [task])
    const removed = floor.robots[0]
    const remaining = agents.filter((agent) => agent.id !== removed.agentId)
    floor.sync(remaining, [task])
    const appearances = floor.robots.map((b) => [b.agentId, b.type, b.cw])
    floor.sync([...remaining.reverse(), { ...worker, id: 'new-agent' }], [task])
    expect(floor.robots.find((b) => b.agentId === 'new-agent')?.type).toBe(removed.type)
    expect(
      floor.robots.filter((b) => b.agentId !== 'new-agent').map((b) => [b.agentId, b.type, b.cw]),
    ).toEqual(appearances)
  })

  it('keeps working on the linked task rather than randomly taking a break', () => {
    const floor = new FactoryState(scene)
    floor.sync([worker], [task])
    advance(floor, 120)
    const b = floor.robots[0]
    expect(b.working).toBe(true)
    expect(b.mode).toBe('work')
    expect(b.taskId).toBe(task.id)
    expect(b.actT).toBeGreaterThan(0.25)
    expect(pickFrame(b, 1)[0]).toBe('work')
    const spot = scene.spots.find((s) => s.tile[0] === b.tc && s.tile[1] === b.tr)
    expect(spot).toBeDefined()
    expect(b.yaw).toBe(floor.paths.yawToTable(b.tc, b.tr))
  })

  it('does not use worker_id to assign another slot’s task', () => {
    const floor = new FactoryState(scene)
    floor.sync([{ ...worker, taskId: null }], [task])
    advance(floor, 90)
    expect(floor.robots[0].working).toBe(false)
    expect(floor.robots[0].mode).toBe('break')
  })

  it.each(['done', 'failed', 'cancelled'] as const)('releases a %s task', (state) => {
    const floor = new FactoryState(scene)
    floor.sync([worker], [task])
    floor.settle()
    expect(floor.robots[0].working).toBe(true)
    floor.sync([worker], [{ ...task, state }])
    advance(floor, 60)
    expect(floor.robots[0].working).toBe(false)
    expect(floor.robots[0].mode).toBe('break')
  })

  it('reacts to idle/error and new assignments without recreating the robot', () => {
    const floor = new FactoryState(scene)
    floor.sync([worker], [task])
    floor.settle()
    const b = floor.robots[0]
    for (const state of ['idle', 'error'] as const) {
      floor.sync([{ ...worker, state }], [task])
      advance(floor, 30)
      expect(b.working).toBe(false)
    }
    floor.sync([{ ...worker, taskId: 't-2' }], [{ ...task, id: 't-2' }])
    advance(floor, 30)
    expect(floor.robots[0]).toBe(b)
    expect(b.working).toBe(true)
    expect(b.taskId).toBe('t-2')
  })

  it('preserves position, path, and appearance on unrelated updates', () => {
    const floor = new FactoryState(scene)
    floor.sync([worker], [task])
    advance(floor, 1)
    const before = structuredClone(floor.robots[0])
    floor.sync([worker], [task, { id: 'unrelated', state: 'pending' }])
    expect(floor.robots[0]).toEqual(before)
  })

  it('uses distinct work seats and reallocates a released seat to overflow agents', () => {
    const floor = new FactoryState(scene)
    const agents = Array.from({ length: 10 }, (_, i) => ({ ...worker, id: `a-${i}` }))
    floor.sync(agents, [task])
    floor.settle()
    const working = floor.robots.filter((b) => b.working)
    expect(working).toHaveLength(scene.spots.length)
    expect(new Set(working.map((b) => `${b.tc},${b.tr}`)).size).toBe(working.length)
    const removed = working[0].agentId
    floor.sync(
      agents.filter((agent) => agent.id !== removed),
      [task],
    )
    floor.settle()
    expect(floor.robots.filter((b) => b.working)).toHaveLength(scene.spots.length)
  })

  it('retargets mid-walk without stepping through a blocked tile', () => {
    const floor = new FactoryState(scene)
    floor.sync([worker], [task])
    for (let i = 0; i < 600; i++) {
      floor.sync([{ ...worker, state: i % 30 < 15 ? 'working' : 'idle' }], [task])
      floor.step(1 / 30)
      const b = floor.robots[0]
      expect(floor.paths.geo.walkable(Math.round(b.c), Math.round(b.r))).toBe(true)
      const path = [[b.c, b.r], ...b.path]
      for (let j = 1; j < path.length; j++) {
        const [c, r] = path[j]
        expect(c === path[j - 1][0] || r === path[j - 1][1]).toBe(true)
      }
    }
  })

  it('idle agents visit coffee/board but never produce work or widgets', () => {
    const floor = new FactoryState(scene)
    floor.sync([{ ...worker, state: 'idle', taskId: null }], [])
    const acts = new Set()
    for (let i = 0; i < 30 * 180; i++) {
      floor.step(1 / 30)
      expect(floor.robots[0].working).toBe(false)
      if (floor.robots[0].act) acts.add(floor.robots[0].act)
    }
    expect(acts).toEqual(new Set(['coffee', 'board']))
  })
})
