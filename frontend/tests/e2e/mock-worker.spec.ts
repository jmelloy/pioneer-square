/**
 * Example e2e flow against a running mock worker.
 *
 * This suite only runs when `MOCK_WORKER_E2E` is set in the environment.
 * Spinning up the backend + mock worker + Vite + chromium isn't free; the
 * default `npm test` job stays cheap.
 *
 * Prerequisite (run in three shells before invoking this spec):
 *
 *   # backend
 *   cd backend && uvicorn main:app --port 8000
 *
 *   # mock worker (after creating a guild in the UI and copying its id)
 *   pioneer-worker --mock --backend-url http://localhost:8000 \
 *                  --guild-id <id> --mock-api-port 9100
 *
 *   # frontend dev server
 *   cd frontend && npm run dev
 *
 * Then run:
 *
 *   MOCK_WORKER_E2E=1 GUILD_ID=<id> npx vitest run tests/e2e/mock-worker.spec.ts
 *
 * The point of this file is to *document the integration pattern*. Real
 * suites should follow this skeleton and add assertions about how the UI
 * renders each state transition.
 */
import { chromium, type Page } from 'playwright'
import { describe, it, beforeAll, afterAll, expect } from 'vitest'
import { existsSync } from 'fs'

import { MockWorkerClient } from './mock-worker-helpers'

const ENABLED = !!process.env.MOCK_WORKER_E2E
const FRONTEND_URL = process.env.FRONTEND_URL ?? 'http://127.0.0.1:5173'
const MOCK_URL = process.env.MOCK_WORKER_URL ?? 'http://127.0.0.1:9100'
const GUILD_ID = process.env.GUILD_ID ?? ''
const LOGIN_TOKEN = process.env.LOGIN_TOKEN ?? 'test-token'

function findChromium(): string | null {
  const env = process.env.PLAYWRIGHT_CHROMIUM_PATH
  if (env && existsSync(env)) return env
  const candidates = [
    '/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
    '/opt/pw-browsers/chromium/chrome-linux/chrome',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
    '/usr/bin/google-chrome',
  ]
  for (const c of candidates) {
    if (existsSync(c)) return c
  }
  return null
}

const CHROMIUM_PATH = findChromium()
const shouldRun = ENABLED && CHROMIUM_PATH && GUILD_ID
const describeMaybe = shouldRun ? describe : describe.skip

let browser: Awaited<ReturnType<typeof chromium.launch>>
let page: Page
let mock: MockWorkerClient

describeMaybe('mock worker drives the UI', () => {
  beforeAll(async () => {
    mock = new MockWorkerClient(MOCK_URL)
    browser = await chromium.launch({
      executablePath: CHROMIUM_PATH!,
      args: ['--no-sandbox', '--disable-dev-shm-usage'],
    })
    page = await browser.newPage()
    await page.addInitScript((token) => {
      localStorage.setItem('login_token', token)
      localStorage.setItem('user', JSON.stringify({ login: 'tester', avatar_url: '', id: 1 }))
    }, LOGIN_TOKEN)
    await page.goto(`${FRONTEND_URL}/guild/${GUILD_ID}`, {
      waitUntil: 'networkidle',
      timeout: 15_000,
    })
  }, 60_000)

  afterAll(async () => {
    await page?.close()
    await browser?.close()
  })

  it('renders an agent transitioning idle → thinking → working → idle', async () => {
    const status = await mock.status()
    expect(status.slots.length).toBeGreaterThan(0)
    const agent = status.slots[0]

    // Drive the slot through a sequence and assert the UI reflects each step.
    await mock.setState(agent.agentId, 'thinking', { activity: 'reading' })
    await expect
      .poll(() => page.locator(`[data-agent-id="${agent.agentId}"]`).getAttribute('data-state'))
      .toBe('thinking')

    await mock.setState(agent.agentId, 'working', { activity: 'editing' })
    await expect
      .poll(() => page.locator(`[data-agent-id="${agent.agentId}"]`).getAttribute('data-state'))
      .toBe('working')

    await mock.setState(agent.agentId, 'idle')
    await expect
      .poll(() => page.locator(`[data-agent-id="${agent.agentId}"]`).getAttribute('data-state'))
      .toBe('idle')
  }, 30_000)

  it('completes a backend-assigned task via the mock control API', async () => {
    // Assumes the test has separately POSTed a task via /guilds/.../workers/.../tasks
    // and stored the resulting id in TASK_ID. In a real suite this would be a
    // single helper that creates the task and hands the id back.
    const taskId = process.env.TASK_ID
    if (!taskId) {
      console.warn('TASK_ID not set — skipping the completion path')
      return
    }
    await mock.waitForActiveTask(taskId, 5000)
    await mock.completeTask(taskId, {
      prUrl: 'https://example.com/pr/42',
      lastText: 'done',
    })
    // The frontend should update the task row to awaiting-review with the PR.
    await expect
      .poll(() => page.locator(`[data-task-id="${taskId}"]`).getAttribute('data-state'))
      .toBe('awaiting-review')
  }, 30_000)
})
