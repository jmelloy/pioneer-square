/**
 * Helpers for driving a running `pioneer-worker --mock` instance from a
 * Playwright e2e test. The mock worker exposes an HTTP control API
 * (default 127.0.0.1:9100) — see `worker/README.md` for the protocol.
 *
 * Usage:
 *
 *   const mock = new MockWorkerClient('http://127.0.0.1:9100')
 *   const { slots } = await mock.status()
 *   await mock.setState(slots[0].agentId, 'thinking', { activity: 'reading' })
 *   await mock.emitOutput(slots[0].agentId, '[mock] reading file', { taskId })
 *   await mock.completeTask(taskId, { prUrl: 'https://example.com/pr/1' })
 */

export type AgentState = 'idle' | 'thinking' | 'working' | 'busy' | 'error' | 'offline'

export interface SlotSnapshot {
  agentId: string
  agentName: string
  state: AgentState
  activity: string | null
  taskId: string | null
}

export interface StatusSnapshot {
  workerId: string
  workerName: string
  guildId: string
  slots: SlotSnapshot[]
  activeTasks: string[]
  queueDepth: number
}

export interface CompleteOptions {
  branch?: string
  prUrl?: string
  stopReason?: string
  lastText?: string
  sessionId?: string
}

export interface FailOptions {
  stopReason?: string
  lastMessage?: string
}

export class MockWorkerClient {
  constructor(private readonly baseUrl: string) {}

  async status(): Promise<StatusSnapshot> {
    return this.request<StatusSnapshot>('GET', '/')
  }

  async setState(
    agentId: string,
    state: AgentState,
    opts: { activity?: string; taskId?: string } = {},
  ): Promise<SlotSnapshot> {
    return this.request<SlotSnapshot>('POST', `/agents/${agentId}/state`, {
      state,
      ...opts,
    })
  }

  async emitOutput(
    agentId: string,
    line: string,
    opts: { taskId?: string; detail?: Record<string, unknown> } = {},
  ): Promise<void> {
    await this.request('POST', `/agents/${agentId}/output`, { line, ...opts })
  }

  async completeTask(taskId: string, opts: CompleteOptions = {}): Promise<void> {
    await this.request('POST', `/tasks/${taskId}/complete`, opts)
  }

  async failTask(taskId: string, opts: FailOptions = {}): Promise<void> {
    await this.request('POST', `/tasks/${taskId}/fail`, opts)
  }

  async shutdown(): Promise<void> {
    await this.request('POST', '/control/shutdown')
  }

  /**
   * Poll the mock until a slot reaches a target state, or throw.
   * Useful for asserting "the slot picked up the task" before driving state.
   */
  async waitForSlotState(
    agentId: string,
    state: AgentState,
    timeoutMs = 5000,
  ): Promise<SlotSnapshot> {
    const deadline = Date.now() + timeoutMs
    let last: SlotSnapshot | undefined
    while (Date.now() < deadline) {
      const snap = await this.status()
      last = snap.slots.find((s) => s.agentId === agentId)
      if (last && last.state === state) return last
      await new Promise((r) => setTimeout(r, 50))
    }
    throw new Error(
      `slot ${agentId} did not reach state=${state} within ${timeoutMs}ms (last=${
        last ? JSON.stringify(last) : 'unknown'
      })`,
    )
  }

  /** Wait until a task is registered as active (the agent loop is parked on it). */
  async waitForActiveTask(taskId: string, timeoutMs = 5000): Promise<void> {
    const deadline = Date.now() + timeoutMs
    while (Date.now() < deadline) {
      const snap = await this.status()
      if (snap.activeTasks.includes(taskId)) return
      await new Promise((r) => setTimeout(r, 50))
    }
    throw new Error(`task ${taskId} did not become active within ${timeoutMs}ms`)
  }

  private async request<T = unknown>(
    method: 'GET' | 'POST',
    path: string,
    body?: unknown,
  ): Promise<T> {
    const resp = await fetch(`${this.baseUrl}${path}`, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    })
    const text = await resp.text()
    const data = text ? (JSON.parse(text) as unknown) : ({} as unknown)
    if (!resp.ok) {
      throw new Error(`mock-worker ${method} ${path} -> ${resp.status}: ${text || '(empty)'}`)
    }
    return data as T
  }
}
