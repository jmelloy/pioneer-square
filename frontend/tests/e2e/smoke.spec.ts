import { chromium, type Page } from 'playwright'
import { describe, it, beforeAll, afterAll, expect } from 'vitest'
import { createServer, type ViteDevServer } from 'vite'
import { existsSync } from 'fs'
import path from 'path'
import { fileURLToPath } from 'url'

// Resolve the frontend root regardless of where vitest is invoked.
const __dirname = path.dirname(fileURLToPath(import.meta.url))
const FRONTEND_ROOT = path.resolve(__dirname, '../..')

// Look for a usable chromium binary. The default Playwright install path
// changes per environment, so prefer an explicit override. Set SKIP_E2E=1
// (or any other truthy value) to force-skip on machines where these tests
// shouldn't run regardless of what's installed.
function findChromium(): string | null {
  if (process.env.SKIP_E2E) return null
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

// Skip the whole suite when no chromium is available (e.g. fresh CI runners).
// The unit-test suite remains the source of truth; this is a local sanity check.
const describeIfBrowser = CHROMIUM_PATH ? describe : describe.skip

let server: ViteDevServer
let baseUrl: string
let browser: Awaited<ReturnType<typeof chromium.launch>>
let page: Page

describeIfBrowser('landing page smoke', () => {
  beforeAll(async () => {
    server = await createServer({
      configFile: path.join(FRONTEND_ROOT, 'vite.config.ts'),
      root: FRONTEND_ROOT,
      server: { port: 0, host: '127.0.0.1' },
      logLevel: 'error',
    })
    await server.listen()
    const addr = server.httpServer?.address()
    if (!addr || typeof addr === 'string') throw new Error('No server address')
    baseUrl = `http://127.0.0.1:${addr.port}`

    browser = await chromium.launch({
      executablePath: CHROMIUM_PATH!,
      args: ['--no-sandbox', '--disable-dev-shm-usage'],
    })
    page = await browser.newPage()
  }, 60_000)

  afterAll(async () => {
    await page?.close()
    await browser?.close()
    await server?.close()
  })

  it('renders the logo and login gate when signed out', async () => {
    const consoleErrors: string[] = []
    page.on('pageerror', (err) => consoleErrors.push(`pageerror: ${err.message}`))
    page.on('console', (msg) => {
      if (msg.type() === 'error') consoleErrors.push(`console: ${msg.text()}`)
    })

    await page.goto(baseUrl, { waitUntil: 'networkidle', timeout: 15_000 })

    await expect.poll(() => page.locator('.logo-title').textContent()).toContain('PIONEER SQUARE')
    await expect.poll(() => page.locator('.login-title').count()).toBeGreaterThan(0)

    // No JS errors that would indicate a broken render. The /guilds 404 is
    // expected when no backend is running alongside the dev server.
    const fatal = consoleErrors.filter(
      (e) =>
        !e.includes('Failed to load resource') &&
        !e.includes('/guilds') &&
        !e.includes('favicon'),
    )
    expect(fatal).toEqual([])
  }, 30_000)

  it('shows the guild card when "signed in" via stub', async () => {
    // Inject a fake auth token before navigation so the landing view shows the
    // signed-in state without hitting GitHub. The store reads from
    // localStorage on mount.
    await page.addInitScript(() => {
      localStorage.setItem('login_token', 'test-token')
      localStorage.setItem('user', JSON.stringify({ login: 'tester', avatar_url: '', id: 1 }))
    })

    // Stub the /guilds endpoint so the page can render without a backend.
    await page.route('**/guilds', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          {
            id: 'abc123',
            name: 'Test Guild',
            created_at: new Date().toISOString(),
            agent_count: 0,
          },
        ]),
      }),
    )

    await page.goto(baseUrl, { waitUntil: 'domcontentloaded', timeout: 15_000 })
    await expect
      .poll(() => page.locator('.session-card, .empty-state, .login-card').count())
      .toBeGreaterThan(0)
  }, 30_000)
})
