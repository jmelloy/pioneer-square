<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useAgentsStore } from '../stores/agents'
import { useTasksStore } from '../stores/tasks'
import { useUiStore } from '../stores/ui'
import { FactoryRenderer, loadImage } from './factory-floor/renderer'
import { FactoryState } from './factory-floor/state'
import type { DoohickeyAtlasMeta, RobotAtlasMeta, SceneConfig } from './factory-floor/types'
import sceneJson from './factory-floor/scenes/glued/scene.json'
import robotMeta from './factory-floor/assets/robots-atlas.json'
import dooMeta from './factory-floor/assets/doohickeys.json'
import backgroundUrl from './factory-floor/scenes/glued/assets/background.png'
import robotsUrl from './factory-floor/assets/robots-atlas.png'
import doohickeysUrl from './factory-floor/assets/doohickeys.png'
import maskUrl from './factory-floor/scenes/glued/assets/prop-mask.png'

const props = withDefaults(defineProps<{ paused?: boolean; showZones?: boolean }>(), {
  paused: undefined,
})
const agentsStore = useAgentsStore()
const tasksStore = useTasksStore()
const uiStore = useUiStore()
const scene = sceneJson as unknown as SceneConfig
const { width: W, height: H } = scene.image
const floor = new FactoryState(scene)
const agents = computed(() => agentsStore.agents.filter((agent) => agent.state !== 'offline'))
const taskById = computed(() => new Map(tasksStore.tasks.map((task) => [task.id, task])))
const positions = ref<Record<string, [number, number]>>({})
const wrap = ref<HTMLElement | null>(null)
const canvas = ref<HTMLCanvasElement | null>(null)
const scale = ref(1)
const loadError = ref('')
const reducedMotion = ref(false)
const isPaused = computed(() => props.paused ?? reducedMotion.value)
let renderer: FactoryRenderer | null = null
let observer: ResizeObserver | null = null
let media: MediaQueryList | null = null
let raf = 0
let last = 0
let clock = 0
let disposed = false

function measure() {
  if (!wrap.value || disposed) return
  const { clientWidth, clientHeight } = wrap.value
  if (clientWidth <= 0 || clientHeight <= 0) return
  scale.value = clientHeight / H
}

function draw() {
  if (reducedMotion.value && props.paused === undefined) floor.settle()
  renderer?.render(clock, floor.robots, { showZones: props.showZones })
  positions.value = Object.fromEntries(
    floor.robots.map((b) => [b.agentId, floor.paths.geo.tileCentre(b.c, b.r)]),
  )
}

// Read only semantic fields: terminal output must not reroute robots.
watch(
  () => ({
    agents: agents.value.map(({ id, state, taskId }) => ({ id, state, taskId })),
    tasks: tasksStore.tasks.map(({ id, state }) => ({ id, state })),
  }),
  ({ agents, tasks }) => {
    floor.sync(agents, tasks)
    draw()
  },
  { immediate: true },
)
watch([isPaused, () => props.showZones], draw)

function frame(time: number) {
  if (disposed) return
  const dt = last ? Math.min(0.1, (time - last) / 1000) : 0
  last = time
  if (!isPaused.value) {
    clock += dt
    floor.step(dt)
  }
  draw()
  raf = requestAnimationFrame(frame)
}

function motionChanged() {
  reducedMotion.value = media?.matches ?? false
}

onMounted(async () => {
  media = window.matchMedia?.('(prefers-reduced-motion: reduce)') ?? null
  motionChanged()
  media?.addEventListener('change', motionChanged)
  measure()
  observer = new ResizeObserver(measure)
  if (wrap.value) observer.observe(wrap.value)
  try {
    const [background, robots, doohickeys, propMask] = await Promise.all([
      loadImage(backgroundUrl),
      loadImage(robotsUrl),
      loadImage(doohickeysUrl),
      loadImage(maskUrl),
    ])
    if (disposed || !canvas.value) return
    renderer = new FactoryRenderer(
      canvas.value,
      scene,
      { background, robots, doohickeys, propMask },
      robotMeta as unknown as RobotAtlasMeta,
      dooMeta as unknown as DoohickeyAtlasMeta,
    )
    draw()
    raf = requestAnimationFrame(frame)
  } catch (error) {
    if (!disposed)
      loadError.value = error instanceof Error ? error.message : 'Unable to load factory floor'
  }
})

onBeforeUnmount(() => {
  disposed = true
  cancelAnimationFrame(raf)
  observer?.disconnect()
  media?.removeEventListener('change', motionChanged)
  renderer = null
})

function agentTitle(agent: (typeof agents.value)[number]) {
  const task = agent.taskId ? taskById.value.get(agent.taskId) : undefined
  return `${agent.name}: ${agent.state}${agent.activity ? ` / ${agent.activity}` : ''}${task ? ` — ${task.name || task.id} (${tasksStore.stateLabel(task.state)})` : ''}`
}

const tickerMessages = computed(() => {
  const messages = agents.value.map(agentTitle)
  tasksStore.tasks
    .filter((task) => !['done', 'failed', 'cancelled'].includes(task.state))
    .slice(0, 4)
    .forEach((task) => messages.push(`TASK: ${task.name || task.id}`))
  return messages.length ? messages : ['AWAITING WORKERS', 'SYSTEMS NOMINAL']
})
</script>

<template>
  <div class="factory-floor">
    <div ref="wrap" class="floor-viewport">
      <div class="floor-stage" :style="{ width: `${W * scale}px`, height: `${H * scale}px` }">
        <canvas
          ref="canvas"
          :width="W"
          :height="H"
          role="img"
          aria-label="Factory workshop with live agents. Select a named robot to open its agent panel."
        />
        <button
          v-for="agent in agents"
          :key="agent.id"
          class="agent-hit"
          :data-agent-id="agent.id"
          :data-state="agent.state"
          :style="{
            left: `${(positions[agent.id]?.[0] ?? 0) * scale}px`,
            top: `${(positions[agent.id]?.[1] ?? 0) * scale}px`,
            width: `${24 * scale}px`,
            height: `${30 * scale}px`,
            zIndex: Math.round(positions[agent.id]?.[1] ?? 0),
          }"
          :title="agentTitle(agent)"
          :aria-label="`Open ${agentTitle(agent)}`"
          @click="uiStore.selectAgent(agent.id)"
        >
          <span class="agent-nametag">{{ agent.name }}</span>
        </button>
      </div>
    </div>
    <div class="factory-info">
      <span class="factory-title">⚙ PIONEER SQUARE WORKSHOP ⚙</span>
      <span>Agents: {{ agents.length }}</span>
    </div>
    <p v-if="loadError" class="load-error" role="alert">{{ loadError }}</p>
    <div class="ticker-tape">
      <div class="ticker-content" :class="{ paused: isPaused }">
        <span v-for="(message, i) in tickerMessages" :key="i">⚙ {{ message }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.factory-floor {
  width: 100%;
  height: 100%;
  position: relative;
  overflow: hidden;
  background: #14131b;
  font-family: var(--font-pixel);
}
.floor-viewport {
  position: absolute;
  inset: 34px 0 28px;
  overflow: hidden;
  mask-image: linear-gradient(to right, transparent, black 6%, black 94%, transparent);
}
.floor-stage {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
}
canvas {
  display: block;
  width: 100%;
  height: 100%;
  image-rendering: pixelated;
}
.agent-hit {
  position: absolute;
  transform: translate(-50%, -100%);
  border: 0;
  padding: 0;
  background: transparent;
  cursor: pointer;
  color: var(--color-brass-light);
  font-family: inherit;
}
.agent-hit:focus-visible {
  outline: 2px solid var(--color-brass);
  outline-offset: 2px;
}
.agent-nametag {
  position: absolute;
  top: 100%;
  left: 50%;
  transform: translateX(-50%);
  font-size: 7px;
  white-space: nowrap;
  max-width: 110px;
  overflow: hidden;
  text-overflow: ellipsis;
  background: #14131bcf;
  padding: 2px;
}
.agent-hit:hover .agent-nametag,
.agent-hit:focus-visible .agent-nametag {
  color: var(--color-teal);
}
.factory-info {
  position: absolute;
  top: 8px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  gap: 16px;
  white-space: nowrap;
  background: var(--color-bg-secondary);
  border: 1px solid var(--color-brass);
  padding: 4px 10px;
  font-size: 7px;
  color: var(--color-teal);
}
.factory-title {
  color: var(--color-amber);
}
.load-error {
  position: absolute;
  top: 40px;
  color: var(--color-red);
  background: #14131b;
}
.ticker-tape {
  position: absolute;
  bottom: 0;
  width: 100%;
  height: 28px;
  background: var(--color-bg-secondary);
  border-top: 2px solid var(--color-brass-dark);
  overflow: hidden;
  display: flex;
  align-items: center;
}
.ticker-content {
  display: flex;
  white-space: nowrap;
  animation: ticker-scroll 20s linear infinite;
  color: var(--color-amber);
  font-size: 8px;
}
.ticker-content span {
  margin-right: 60px;
}
.ticker-content.paused {
  animation: none;
}
@keyframes ticker-scroll {
  from {
    transform: translateX(100vw);
  }
  to {
    transform: translateX(-100%);
  }
}
</style>
