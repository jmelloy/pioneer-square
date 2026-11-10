import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import FactoryFloor from '../FactoryFloor.vue'
import { useAgentsStore } from '../../stores/agents'
import { useTasksStore } from '../../stores/tasks'
import { useUiStore } from '../../stores/ui'
import type { Agent } from '../../types'

const mocks = vi.hoisted(() => ({ render: vi.fn(), load: vi.fn(), construct: vi.fn() }))
vi.mock('../factory-floor/renderer', () => ({
  loadImage: mocks.load,
  FactoryRenderer: class {
    render = mocks.render
    constructor(...args: unknown[]) {
      mocks.construct(...args)
    }
  },
}))

let frame: FrameRequestCallback
let resize: ResizeObserverCallback
const cancel = vi.fn()
const disconnect = vi.fn()
const removeListener = vi.fn()
const wrappers: ReturnType<typeof mount>[] = []

const worker: Agent = {
  id: 'a-1',
  name: 'Boiler Bot',
  type: 'worker',
  workerId: 'w-1',
  state: 'working',
  taskId: 't-1',
  logs: [],
  joinedAt: '',
}

function mountFloor(props = {}) {
  const wrapper = mount(FactoryFloor, { props })
  wrappers.push(wrapper)
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
  mocks.load.mockResolvedValue({})
  vi.stubGlobal(
    'requestAnimationFrame',
    vi.fn((callback) => {
      frame = callback
      return 42
    }),
  )
  vi.stubGlobal('cancelAnimationFrame', cancel)
  vi.stubGlobal(
    'ResizeObserver',
    class {
      constructor(callback: ResizeObserverCallback) {
        resize = callback
      }
      observe = vi.fn()
      disconnect = disconnect
    },
  )
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: removeListener,
    })),
  )
  useAgentsStore().agents = [{ ...worker }, { ...worker, id: 'offline', state: 'offline' }]
  useTasksStore().tasks = [{ id: 't-1', name: 'Build widget', state: 'working' }]
})

afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  vi.unstubAllGlobals()
})

describe('FactoryFloor', () => {
  it('mounts assets, labels real agents/tasks, and opens the existing agent panel', async () => {
    const wrapper = mountFloor()
    await flushPromises()
    expect(mocks.load).toHaveBeenCalledTimes(4)
    expect(mocks.construct).toHaveBeenCalledOnce()
    expect(wrapper.findAll('.agent-hit')).toHaveLength(1)
    expect(wrapper.find('canvas').attributes('width')).toBe('297')
    expect(wrapper.find('.agent-hit').attributes('aria-label')).toContain('Build widget')
    await wrapper.find('.agent-hit').trigger('click')
    expect(useUiStore().selectedAgentId).toBe('a-1')
    expect(mocks.render).toHaveBeenCalled()
  })

  it('updates from the existing stores without spawning demo agents', async () => {
    const wrapper = mountFloor()
    await flushPromises()
    useAgentsStore().updateAgentState('a-1', 'idle')
    await flushPromises()
    expect(wrapper.find('.agent-hit').attributes('data-state')).toBe('idle')
    expect(mocks.render.mock.lastCall?.[1][0].mode).toBe('break')
    useAgentsStore().agents = []
    await flushPromises()
    expect(wrapper.findAll('.agent-hit')).toHaveLength(0)
    expect(mocks.render.mock.lastCall?.[1]).toEqual([])
  })

  it('freezes positions and every animation clock when paused', async () => {
    const wrapper = mountFloor()
    await flushPromises()
    frame(1000)
    frame(1050)
    await wrapper.setProps({ paused: true })
    const before = structuredClone(mocks.render.mock.lastCall?.slice(0, 2))
    frame(1100)
    frame(1200)
    expect(mocks.render.mock.lastCall?.slice(0, 2)).toEqual(before)
    await wrapper.setProps({ paused: false })
    frame(1300)
    expect(mocks.render.mock.lastCall?.[0]).toBeGreaterThan(before![0])
  })

  it('honors reduced motion, showing current work without movement', async () => {
    vi.stubGlobal(
      'matchMedia',
      vi.fn(() => ({
        matches: true,
        addEventListener: vi.fn(),
        removeEventListener: removeListener,
      })),
    )
    mountFloor()
    await flushPromises()
    frame(1000)
    frame(2000)
    expect(mocks.render.mock.lastCall?.[0]).toBe(0)
    expect(mocks.render.mock.lastCall?.[1][0].working).toBe(true)
    expect(mocks.render.mock.lastCall?.[1][0].actT).toBeGreaterThan(0.25)
    expect(mocks.render.mock.lastCall?.[1][0].path).toEqual([])
  })

  it('zooms at 80% fit, crops toward work spots, and keeps agent targets aligned', async () => {
    const wrapper = mountFloor()
    await flushPromises()
    const viewport = wrapper.find('.floor-viewport').element
    const stage = wrapper.find<HTMLElement>('.floor-stage').element
    const button = wrapper.find<HTMLElement>('.agent-hit').element
    const originalLeft = parseFloat(button.style.left)
    const originalTop = parseFloat(button.style.top)
    for (const [width, height] of [
      [600, 700],
      [200, 700],
      [1800, 2000],
    ]) {
      Object.defineProperties(viewport, {
        clientWidth: { configurable: true, value: width },
        clientHeight: { configurable: true, value: height },
      })
      resize([], {} as ResizeObserver)
      await flushPromises()
      const scale = Math.max(1, Math.floor(Math.min(width / 297, height / 383) / 0.8))
      expect(viewport.scrollLeft).toBeGreaterThanOrEqual(0)
      expect(viewport.scrollLeft).toBeLessThanOrEqual(Math.max(0, 297 * scale - width))
      expect(viewport.scrollTop).toBe(Math.max(0, 383 * scale - height))
      if (width < 297 * scale) expect(viewport.scrollLeft).toBeGreaterThan(0)
      expect(parseFloat(stage.style.width)).toBeCloseTo(297 * scale)
      expect(parseFloat(stage.style.height)).toBeCloseTo(383 * scale)
      expect(parseFloat(button.style.left)).toBeCloseTo(originalLeft * scale)
      expect(parseFloat(button.style.top)).toBeCloseTo(originalTop * scale)
      expect(parseFloat(button.style.width)).toBeCloseTo(24 * scale)
      expect(wrapper.find('canvas').attributes('width')).toBe('297')
    }
    const previousWidth = stage.style.width
    Object.defineProperty(viewport, 'clientWidth', { configurable: true, value: 0 })
    resize([], {} as ResizeObserver)
    await flushPromises()
    expect(stage.style.width).toBe(previousWidth)
  })

  it.each([
    [1000, 612, 1],
    [1000, 613, 2],
    [1000, 919, 2],
    [1000, 920, 3],
    [1000, 1225, 3],
    [1000, 1226, 4],
    [712, 2000, 2],
    [713, 2000, 3],
  ])('uses %ipx × %ipx to select %i× at the 80%% threshold', async (width, height, scale) => {
    const wrapper = mountFloor()
    await flushPromises()
    const viewport = wrapper.find('.floor-viewport').element
    Object.defineProperties(viewport, {
      clientWidth: { configurable: true, value: width },
      clientHeight: { configurable: true, value: height },
    })
    resize([], {} as ResizeObserver)
    await flushPromises()
    const stage = wrapper.find<HTMLElement>('.floor-stage').element
    expect(parseFloat(stage.style.width)).toBe(297 * scale)
    expect(parseFloat(stage.style.height)).toBe(383 * scale)
  })

  it('reports asset failures instead of an unhandled rejection', async () => {
    mocks.load.mockRejectedValueOnce(new Error('Missing atlas'))
    const wrapper = mountFloor()
    await flushPromises()
    expect(wrapper.find('[role="alert"]').text()).toContain('Missing atlas')
    expect(mocks.construct).not.toHaveBeenCalled()
  })

  it('cancels frames, observer and media listener on unmount', async () => {
    const wrapper = mountFloor()
    await flushPromises()
    wrapper.unmount()
    expect(cancel).toHaveBeenCalledWith(42)
    expect(disconnect).toHaveBeenCalled()
    expect(removeListener).toHaveBeenCalledWith('change', expect.any(Function))
    const count = mocks.render.mock.calls.length
    frame(1000)
    expect(mocks.render).toHaveBeenCalledTimes(count)
  })

  it('does not initialize the renderer after unmount during asset loading', async () => {
    let resolve!: (value: unknown) => void
    mocks.load.mockReturnValue(
      new Promise((done) => {
        resolve = done
      }),
    )
    const wrapper = mountFloor()
    wrapper.unmount()
    resolve({})
    await flushPromises()
    expect(mocks.construct).not.toHaveBeenCalled()
    expect(requestAnimationFrame).not.toHaveBeenCalled()
  })
})
