import { chromium, type Page } from 'playwright'
import { describe, it, beforeAll, afterAll, expect } from 'vitest'
import { createServer, type ViteDevServer } from 'vite'
import { existsSync } from 'fs'
import path from 'path'
import { fileURLToPath } from 'url'
import { installTypographyFixture, logs, task } from './typography-fixture'

// Resolve the frontend root regardless of where vitest is invoked.
const __dirname = path.dirname(fileURLToPath(import.meta.url))
const FRONTEND_ROOT = path.resolve(__dirname, '../..')

// Look for a usable chromium binary. The default Playwright install path
// changes per environment, so prefer an explicit override. Set SKIP_E2E=1
// (or any other truthy value) to force-skip on machines where these tests
// shouldn't run regardless of what's installed.
function findChromium(): string | null {
  if (process.env.SKIP_E2E || process.env.CI) return null
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

describeIfBrowser('browser smoke', () => {
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
        !e.includes('Failed to load resource') && !e.includes('/guilds') && !e.includes('favicon'),
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

  it('keeps overflowing issue rows and descriptions inside independently scrollable panes', async () => {
    const now = new Date().toISOString()
    const guild = { id: 'scroll', name: 'Scroll test', primary_repo: 'test/factory', agents: [] }
    const issues = Array.from({ length: 25 }, (_, index) => ({
      id: index + 1,
      number: index + 1,
      title: `Issue ${index + 1}`,
      state: 'open',
      labels: [],
      created_at: now,
      user: { login: 'tester', avatar_url: '' },
      body: Array.from({ length: 100 }, (_, n) => `Paragraph ${n}: long description.`).join('\n\n'),
    }))
    await page.addInitScript(() => {
      localStorage.setItem('auth_token', 'test-token')
      localStorage.setItem('auth_user', JSON.stringify({ login: 'tester', id: '1' }))
      localStorage.setItem('gh_token', 'test-token')
    })
    await page.route('**/*', async (route) => {
      const url = new URL(route.request().url())
      let data: unknown
      if (url.hostname === 'api.github.com') data = issues
      else if (url.pathname === '/guilds') data = [guild]
      else if (url.pathname === '/guilds/scroll') data = guild
      else if (url.pathname.endsWith('/tasks/tree')) data = { conversations: [], ungrouped: [] }
      else if (url.pathname.startsWith('/api/issues/')) data = { issue: issues[0], comments: [] }
      else if (url.pathname.startsWith('/guilds/') || url.pathname.startsWith('/api/guilds/'))
        data = []
      if (data === undefined) await route.continue()
      else await route.fulfill({ json: data })
    })
    await page.setViewportSize({ width: 1280, height: 720 })
    await page.goto(`${baseUrl}/scroll`)
    await page.getByRole('button', { name: /Projects \/ Issues/ }).click()
    await page.locator('.issue-title-row').waitFor()

    async function expectScrollable(selector: string) {
      const result = await page.locator(selector).evaluate((el) => {
        el.scrollTop = el.scrollHeight
        const bottom = el.getBoundingClientRect().bottom
        const boardBottom = document.querySelector('.project-main')!.getBoundingClientRect().bottom
        return { scrollTop: el.scrollTop, bottom, boardBottom }
      })
      expect(result.scrollTop).toBeGreaterThan(0)
      expect(result.bottom).toBeLessThanOrEqual(result.boardBottom)
    }

    await expectScrollable('.project-table-scroll')
    await expectScrollable('.issue-tab-content')
    await page.setViewportSize({ width: 390, height: 844 })
    await expectScrollable('.project-table-scroll')
    await page.locator('.issue-select').first().click()
    await expectScrollable('.issue-tab-content')
  }, 30_000)

  it('keeps workspace text readable and contained across phone and desktop widths', async () => {
    const now = new Date().toISOString()
    const conversation = {
      id: 1,
      name: `Investigate ${'long-unbroken-title'.repeat(8)}`,
      status: 'active',
      created_at: now,
      updated_at: now,
    }
    const messages = [
      {
        type: 'chat',
        from: 'foreman',
        conversationId: 1,
        createdAt: now,
        content: `# Summary\n\n## Findings\n\n### Next steps\n\nRead this message and ${'long-url-segment'.repeat(30)}\n\n\`\`\`\n${'code'.repeat(100)}\n\`\`\``,
      },
    ]
    const guild = { id: 'type', name: 'Typography workspace', agents: [], messages }
    await page.addInitScript(() => {
      localStorage.setItem('auth_token', 'test-token')
      localStorage.setItem('auth_user', JSON.stringify({ login: 'tester', id: '1' }))
    })
    await page.route('**/*', async (route) => {
      const url = new URL(route.request().url())
      let data: unknown
      if (url.pathname === '/guilds') data = [guild]
      else if (url.pathname === '/guilds/type') data = guild
      else if (url.pathname.endsWith('/conversations/1/messages')) data = messages
      else if (url.pathname.endsWith('/conversations/1')) data = conversation
      else if (url.pathname.endsWith('/conversations')) data = [conversation]
      else if (url.pathname.endsWith('/tasks/tree')) data = { conversations: [], ungrouped: [] }
      else if (url.pathname.startsWith('/guilds/') || url.pathname.startsWith('/api/guilds/'))
        data = []
      if (data === undefined) await route.continue()
      else await route.fulfill({ json: data })
    })

    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto(`${baseUrl}/type`)
    await page.locator('.conversation-row .open-btn').click()
    await page.locator('.conversation-tab').click()
    await page.locator('.conversation-detail .msg-content h1').waitFor()

    for (const width of [1440, 1024, 700, 390, 320]) {
      await page.setViewportSize({ width, height: 900 })
      if (width <= 1024)
        await page.locator('.mobile-tab-bar').getByRole('button', { name: 'Work' }).click()
      const metrics = await page.locator('.conversation-detail').evaluate((pane) => {
        const content = pane.querySelector('.msg-content')!
        const size = (selector: string) =>
          parseFloat(getComputedStyle(pane.querySelector(selector)!).fontSize)
        const bounds = pane.getBoundingClientRect()
        return {
          body: parseFloat(getComputedStyle(content).fontSize),
          input: size('.reply-input'),
          h1: size('h1'),
          h2: size('h2'),
          h3: size('h3'),
          width: bounds.width,
          right: bounds.right,
          overflow: content.scrollWidth - content.clientWidth,
          titleOverflow:
            pane.querySelector('.pane-title')!.scrollWidth -
            pane.querySelector('.pane-title')!.clientWidth,
        }
      })
      expect(metrics.body).toBeGreaterThanOrEqual(width <= 700 ? 16 : 15)
      expect(metrics.input).toBeGreaterThanOrEqual(16)
      expect(metrics.h1).toBeGreaterThan(metrics.h2)
      expect(metrics.h2).toBeGreaterThan(metrics.h3)
      expect(metrics.width).toBeGreaterThan(0)
      expect(metrics.right).toBeLessThanOrEqual(width)
      expect(metrics.overflow).toBeLessThanOrEqual(1)
      expect(metrics.titleOverflow).toBeLessThanOrEqual(1)

      if (width <= 1024)
        await page.locator('.mobile-tab-bar').getByRole('button', { name: 'Chat' }).click()
      const input = page.locator('.chat-input')
      expect(
        await input.evaluate((el) => parseFloat(getComputedStyle(el).fontSize)),
      ).toBeGreaterThanOrEqual(16)
      if (width <= 700) {
        const send = await page.locator('.chat-input-row .send-btn').boundingBox()
        expect(send!.height).toBeGreaterThanOrEqual(44)
      }
    }
  }, 30_000)

  it('keeps settings, preferences, and spawn controls readable without horizontal overflow', async () => {
    const guild = { id: 'settings', name: 'A guild with a long name '.repeat(3), agents: [] }
    const env = [{ key: 'EXAMPLE_SETTING', value: 'example-value' }]
    await page.addInitScript(() => {
      localStorage.setItem('auth_token', 'test-token')
      localStorage.setItem('auth_user', JSON.stringify({ login: 'tester', id: '1' }))
      localStorage.setItem('gh_token', 'test-token')
    })
    await page.route('**/*', async (route) => {
      const url = new URL(route.request().url())
      let data: unknown
      if (url.pathname === '/guilds') data = [guild]
      else if (url.pathname === '/guilds/settings') data = guild
      else if (url.pathname.endsWith('/foreman-config')) data = { env_vars: env }
      else if (url.pathname.endsWith('/spawn-settings')) data = { envVars: env }
      else if (url.pathname.endsWith('/spawn-defaults'))
        data = { repos: [], tools: [], agent_count: 4 }
      else if (url.pathname.endsWith('/spawn-credentials')) data = { guild_env_vars: [] }
      else if (url.pathname.endsWith('/members'))
        data = [
          { user_id: '1', github_login: 'tester', role: 'owner' },
          { user_id: '2', github_login: 'long-member-name'.repeat(8), role: 'member' },
        ]
      else if (url.pathname.endsWith('/discord/status'))
        data = { gateway: { enabled: false, running: false }, allowed_role_ids: [] }
      else if (url.pathname.endsWith('/tasks/tree')) data = { conversations: [], ungrouped: [] }
      else if (url.hostname === 'api.github.com' || url.pathname === '/api/models') data = []
      else if (url.pathname.startsWith('/guilds/') || url.pathname.startsWith('/api/guilds/'))
        data = []
      if (data === undefined) await route.continue()
      else await route.fulfill({ json: data })
    })
    await page.goto(`${baseUrl}/settings`)

    async function expectReadableDialog(width: number) {
      const metrics = await page.locator('.settings-panel').evaluate((panel) => {
        const content = panel.querySelector('.settings-content')!
        const inputs = Array.from(
          panel.querySelectorAll('input:not([type="checkbox"]), select, textarea'),
        ).filter((el) => el.getBoundingClientRect().width > 0)
        return {
          right: panel.getBoundingClientRect().right,
          overflow: content.scrollWidth - content.clientWidth,
          titleSize: parseFloat(
            getComputedStyle(panel.querySelector('.settings-panel-title')!).fontSize,
          ),
          inputs: inputs.map((el) => parseFloat(getComputedStyle(el).fontSize)),
          buttons: Array.from(panel.querySelectorAll('button'))
            .filter((el) => el.getBoundingClientRect().width > 0)
            .map((el) => el.getBoundingClientRect().height),
        }
      })
      expect(metrics.right).toBeLessThanOrEqual(width)
      expect(metrics.overflow).toBeLessThanOrEqual(1)
      expect(metrics.titleSize).toBeGreaterThanOrEqual(18)
      for (const size of metrics.inputs) expect(size).toBeGreaterThanOrEqual(16)
      if (width <= 700)
        for (const height of metrics.buttons) expect(height).toBeGreaterThanOrEqual(44)
    }

    for (const width of [1440, 700, 390, 320]) {
      await page.setViewportSize({ width, height: 900 })
      await page.getByTitle('Guild settings', { exact: true }).click()
      for (const tab of ['General', 'Foreman', 'Worker Settings', 'Members', 'Discord']) {
        await page.locator('.settings-tabs').getByRole('button', { name: tab, exact: true }).click()
        await expectReadableDialog(width)
      }
      await page.keyboard.press('Escape')
      await page.getByTitle('Preferences for tester', { exact: true }).click()
      await page.locator('.settings-tabs').getByRole('button', { name: 'Worker Settings' }).click()
      await page.locator('.uws-input').first().waitFor()
      await expectReadableDialog(width)
      await page.keyboard.press('Escape')
      if (width <= 1024)
        await page.locator('.mobile-tab-bar').getByRole('button', { name: 'Chat' }).click()
      await page.getByTitle('Launch a new worker container', { exact: true }).click()
      for (const tab of ['General', 'Tools', 'Environment']) {
        await page.locator('.settings-tabs').getByRole('button', { name: tab, exact: true }).click()
        await expectReadableDialog(width)
      }
      const input = page.locator('.spawn-env-key').first()
      await input.focus()
      expect(await input.evaluate((el) => getComputedStyle(el).outlineStyle)).toBe('solid')
      await page.keyboard.press('Escape')
    }
  }, 30_000)

  it('uses readable standalone log typography without widening the page', async () => {
    await installTypographyFixture(page)
    await page.goto(`${baseUrl}/task/${task.id}/log`)
    await page.locator('.log-text--markdown h1').waitFor()
    for (const width of [1440, 700, 390, 320]) {
      await page.setViewportSize({ width, height: 900 })
      const metrics = await page.locator('.task-log').evaluate((el) => {
        const prose = el.querySelector('.markdown-prose')!
        const size = (selector: string) =>
          parseFloat(getComputedStyle(el.querySelector(selector)!).fontSize)
        return {
          body: parseFloat(getComputedStyle(prose).fontSize),
          proseColor: getComputedStyle(prose).color,
          textColor: getComputedStyle(el).color,
          h1: size('h1'),
          h2: size('h2'),
          h3: size('h3'),
          raw: getComputedStyle(el.querySelector('.log-text:not(.log-text--markdown)')!).fontFamily,
          overflow: el.scrollWidth - el.clientWidth,
          proseOverflow: prose.scrollWidth - prose.clientWidth,
          logHeight: el.querySelector('.pane-body')!.clientHeight,
        }
      })
      expect(metrics.body).toBeGreaterThanOrEqual(width <= 700 ? 16 : 15)
      expect(metrics.h1).toBeGreaterThan(metrics.h2)
      expect(metrics.h2).toBeGreaterThan(metrics.h3)
      expect(metrics.raw).toContain('monospace')
      expect(metrics.proseColor).toBe(metrics.textColor)
      expect(metrics.overflow).toBeLessThanOrEqual(1)
      expect(metrics.proseOverflow).toBeLessThanOrEqual(1)
      expect(metrics.logHeight).toBeGreaterThan(0)
      expect(await page.locator('.markdown-prose pre code').textContent()).toContain(
        '    preserve_indentation',
      )
      expect(await page.locator('.log-text:not(.log-text--markdown)').first().textContent()).toBe(
        logs[1]!.line,
      )
    }
    await page.locator('.log-line--expandable').click()
    expect(await page.locator('.log-detail-body').textContent()).toContain('    echo example')
  }, 30_000)

  it('keeps task actions, metadata, and teleported debug text readable on phones', async () => {
    page.setDefaultTimeout(5000)
    await installTypographyFixture(page)
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto(`${baseUrl}/${task.guild_id}`)
    await page.getByRole('button', { name: 'All', exact: true }).click()
    await page.locator('.task-row').first().waitFor()
    for (const width of [1440, 700, 390, 320]) {
      await page.setViewportSize({ width, height: 900 })
      if (width <= 1024)
        await page.locator('.mobile-tab-bar').getByRole('button', { name: 'Tasks' }).click()
      const rows = await page
        .locator('.task-row')
        .evaluateAll((els) =>
          els.map((el) => ({ overflow: el.scrollWidth - el.clientWidth, height: el.clientHeight })),
        )
      expect(rows.length).toBeGreaterThan(6)
      for (const row of rows) {
        expect(row.overflow).toBeLessThanOrEqual(1)
        expect(row.height).toBeGreaterThanOrEqual(43)
      }
      await page.locator('.task-name').filter({ hasText: task.name }).click()
      if (width <= 1024)
        await page.locator('.mobile-tab-bar').getByRole('button', { name: 'Work' }).click()
      await page.locator('.followup-input').waitFor()
      const metrics = await page.locator('.log-pane').evaluate((el) => ({
        overflow: el.scrollWidth - el.clientWidth,
        logHeight: el.querySelector('.pane-body')!.clientHeight,
        input: parseFloat(getComputedStyle(el.querySelector('.followup-input')!).fontSize),
        label: parseFloat(getComputedStyle(el.querySelector('.phase-badge')!).fontSize),
      }))
      expect(metrics.overflow).toBeLessThanOrEqual(1)
      expect(metrics.logHeight).toBeGreaterThan(0)
      expect(metrics.input).toBeGreaterThanOrEqual(16)
      expect(metrics.label).toBeGreaterThanOrEqual(12)
    }

    await page.setViewportSize({ width: 1440, height: 900 })
    await page.getByTitle('Debug: foreman context').click()
    await page.locator('.debug-pre').waitFor()
    for (const width of [1440, 700, 390, 320]) {
      await page.setViewportSize({ width, height: 900 })
      const metrics = await page.locator('.debug-sidebar').evaluate((el) => ({
        inWorkspace: Boolean(el.closest('.app-layout')),
        overflow: el.scrollWidth - el.clientWidth,
        text: parseFloat(getComputedStyle(el.querySelector('.debug-text')!).fontSize),
        role: parseFloat(getComputedStyle(el.querySelector('.debug-role')!).fontSize),
        code: getComputedStyle(el.querySelector('.debug-pre')!).fontFamily,
      }))
      expect(metrics.inWorkspace).toBe(false)
      expect(metrics.overflow).toBeLessThanOrEqual(1)
      expect(metrics.text).toBeGreaterThanOrEqual(width <= 700 ? 16 : 15)
      expect(metrics.role).toBeGreaterThanOrEqual(12)
      expect(metrics.code).toContain('monospace')
    }
    await page.locator('.debug-sidebar .close-btn').click()

    await page.setViewportSize({ width: 1440, height: 900 })
    await page.getByTitle('Expand workers', { exact: true }).click()
    await page.locator('.agent-row-name').filter({ hasText: 'Type Agent' }).click()
    await page.locator('.tool-select').selectOption('pi')
    for (const width of [1440, 700, 390, 320]) {
      await page.setViewportSize({ width, height: 900 })
      if (width <= 1024)
        await page.locator('.mobile-tab-bar').getByRole('button', { name: 'Work' }).click()
      const metrics = await page.locator('.actions-panel').evaluate((el) => ({
        overflow: el.scrollWidth - el.clientWidth,
        inputs: Array.from(el.querySelectorAll('input, select')).map((input) =>
          parseFloat(getComputedStyle(input).fontSize),
        ),
      }))
      expect(metrics.overflow).toBeLessThanOrEqual(1)
      for (const size of metrics.inputs) expect(size).toBeGreaterThanOrEqual(16)
    }
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.locator('.worker-row-name').filter({ hasText: 'Type Worker' }).click()
    await page.locator('.expand-toggle').click()
    await page.setViewportSize({ width: 320, height: 900 })
    await page.locator('.cancel-btn').scrollIntoViewIfNeeded()
    expect(
      await page.locator('.log-pane .pane-body').evaluate((el) => el.clientHeight),
    ).toBeGreaterThan(0)
    const actionBounds = await page.locator('.actions-panel').boundingBox()
    const cancelBounds = await page.locator('.cancel-btn').boundingBox()
    expect(cancelBounds!.y + cancelBounds!.height).toBeLessThanOrEqual(
      actionBounds!.y + actionBounds!.height + 1,
    )

    await page.getByRole('button', { name: /Projects \/ Issues/ }).click()
    await page.locator('.issue-select').first().click()
    await page
      .locator('.issue-tab-bar')
      .getByRole('button', { name: /Comments/ })
      .click()
    await page.locator('.comment-date').waitFor()
    expect(
      await page
        .locator('.comment-date')
        .evaluate((el) => parseFloat(getComputedStyle(el).fontSize)),
    ).toBeGreaterThanOrEqual(12)
    const issueOverflow = await page
      .locator('.issue-tab-content')
      .evaluate((el) => el.scrollWidth - el.clientWidth)
    expect(issueOverflow).toBeLessThanOrEqual(1)
  }, 30_000)
})
