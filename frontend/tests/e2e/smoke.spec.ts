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

// Bounded checks for the opaque text/background pairs touched by this pass.
async function expectTextContrast(text: string, background: string) {
  const foreground = await page.locator(text).evaluate((el) => getComputedStyle(el).color)
  const backdrop = await page
    .locator(background)
    .evaluate((el) => getComputedStyle(el).backgroundColor)
  function luminance(color: string) {
    expect(color.startsWith('rgb(')).toBe(true)
    const channels = color
      .match(/[\d.]+/g)!
      .map(Number)
      .map((value) => {
        const channel = value / 255
        return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
      })
    return channels[0]! * 0.2126 + channels[1]! * 0.7152 + channels[2]! * 0.0722
  }
  const values = [luminance(foreground), luminance(backdrop)].sort((a, b) => b - a)
  expect((values[0]! + 0.05) / (values[1]! + 0.05)).toBeGreaterThanOrEqual(4.5)
}

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
    await expectScrollable('.issue-viewer')
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

  it('reflows issue details and comments with touch-sized actions on mobile', async () => {
    page.setDefaultTimeout(5000)
    await installTypographyFixture(page)
    const longText = 'unbroken-identifier'.repeat(5)
    await page.route('**/api/issues/test/factory/1', (route) =>
      route.fulfill({
        json: {
          issue: {
            id: 1,
            number: 1,
            title: longText,
            state: 'open',
            html_url: 'https://github.com/test/factory/issues/1',
            created_at: task.created_at,
            user: { login: longText, avatar_url: 'https://example.com/mobile-test.svg' },
            labels: [{ id: 1, name: longText, color: 'eab900' }],
            body: `${logs[0]!.line}\n\n![Wide image](https://example.com/mobile-test.svg)`,
          },
          comments: [
            {
              id: 1,
              body: logs[0]!.line,
              created_at: task.created_at,
              user: { login: longText, avatar_url: 'https://example.com/mobile-test.svg' },
              html_url: 'https://github.com/test/factory/issues/1#issuecomment-1',
            },
          ],
        },
      }),
    )
    await page.route('https://example.com/mobile-test.svg', (route) =>
      route.fulfill({
        contentType: 'image/svg+xml',
        body: '<svg xmlns="http://www.w3.org/2000/svg" width="1400" height="300"><rect width="1400" height="300" fill="#64748b" /></svg>',
      }),
    )
    await page.goto(`${baseUrl}/${task.guild_id}`)
    await page.getByRole('button', { name: /Projects \/ Issues/ }).click()
    await page.locator('.issue-select').first().click()
    await page.locator('.issue-body img').waitFor()

    for (const width of [320, 375, 390, 700, 701, 768]) {
      await page.setViewportSize({ width, height: 740 })
      for (const tab of ['Details', 'Comments', 'Task Log']) {
        await page
          .locator('.issue-tab-bar')
          .getByRole('button', { name: new RegExp(tab) })
          .click()
        const overflow = await page.locator('.issue-viewer').evaluate((el) =>
          [el, ...el.querySelectorAll('*')]
            .filter(
              (node) =>
                node.tagName !== 'IMG' &&
                node.clientWidth > 0 &&
                node.scrollWidth > node.clientWidth + 1,
            )
            .map((node) => ({
              className: node.className,
              width: node.clientWidth,
              scroll: node.scrollWidth,
              font: getComputedStyle(node).fontSize,
            })),
        )
        expect(overflow, `${width}px ${tab} overflow`).toEqual([])
        expect(await page.locator('.project-table-pane').isVisible()).toBe(false)
        expect(await page.locator('.projects-toolbar').isVisible()).toBe(false)
        expect(
          await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth),
        ).toBeLessThanOrEqual(1)
        const targets = await page
          .locator('.issue-viewer button, .issue-viewer .gh-link, .issue-viewer .gh-link-sm')
          .evaluateAll((els) =>
            els.map((el) => ({
              name: el.textContent,
              width: el.getBoundingClientRect().width,
              height: el.getBoundingClientRect().height,
            })),
          )
        for (const target of targets) {
          expect(target.width, `${width}px ${target.name} width`).toBeGreaterThanOrEqual(44)
          expect(target.height, `${width}px ${target.name} height`).toBeGreaterThanOrEqual(44)
        }
        await page.locator('.ifp-send').scrollIntoViewIfNeeded()
        if (
          process.env.ISSUE_MOBILE_SCREENSHOTS &&
          [320, 768].includes(width) &&
          tab !== 'Task Log'
        ) {
          if (width === 320 && tab === 'Details') {
            await page.locator('.issue-viewer').evaluate((el) => {
              el.scrollTop = 0
            })
            await page.screenshot({
              path: `${process.env.ISSUE_MOBILE_SCREENSHOTS}/issue-320-header.png`,
            })
          }
          await page
            .locator(
              tab === 'Details' ? '.issue-body-section > .section-header' : '.comment-header',
            )
            .scrollIntoViewIfNeeded()
          await page.screenshot({
            path: `${process.env.ISSUE_MOBILE_SCREENSHOTS}/issue-${width}-${tab.toLowerCase()}.png`,
            fullPage: true,
          })
          if (width === 320 && tab === 'Comments') {
            await page.locator('.post-comment-section').scrollIntoViewIfNeeded()
            await page.screenshot({
              path: `${process.env.ISSUE_MOBILE_SCREENSHOTS}/issue-320-comment-composer.png`,
            })
          }
        }
      }
      await page.locator('.issue-title-row .edit-btn').click()
      await page.locator('.title-input').fill('Edited title')
      expect(
        await page.locator('.title-input').evaluate((el) => el.scrollWidth - el.clientWidth),
      ).toBeLessThanOrEqual(1)
      await page.locator('.issue-title-edit .cancel-btn').click()
      await page
        .locator('.issue-tab-bar')
        .getByRole('button', { name: 'Details', exact: true })
        .click()
      await page.locator('.issue-body-section .edit-btn').click()
      await page.locator('.body-textarea').fill('Edited description')
      await page.locator('.body-edit .cancel-btn').click()
    }
  }, 60_000)

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

  it('keeps guild creation readable at 200% text size and contains keyboard focus', async () => {
    await installTypographyFixture(page)
    let createdName = ''
    await page.route('**/guilds', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback()
      createdName = route.request().postDataJSON().name
      await route.fulfill({ json: { id: task.guild_id, name: createdName } })
    })
    await page.setViewportSize({ width: 320, height: 900 })
    await page.goto(baseUrl)
    await page.locator('.session-card').focus()
    await page.keyboard.press('Enter')
    await expect.poll(() => page.url()).toContain(task.guild_id)
    await page.locator('.app-layout .guild-name').waitFor()
    await page.goto(baseUrl)
    const trigger = page.locator('.new-btn')
    await trigger.focus()
    await page.keyboard.press('Enter')
    const dialog = page.getByRole('dialog', { name: 'NEW GUILD', exact: true })
    const input = page.getByLabel('Guild Name (optional)', { exact: true })
    await dialog.waitFor()
    expect(await dialog.evaluate((el) => el.matches(':modal'))).toBe(true)
    expect(await input.evaluate((el) => el === document.activeElement)).toBe(true)
    // Native dialogs may include a browser-chrome focus stop (body), not background controls.
    for (let index = 0; index < 6; index++) {
      await page.keyboard.press('Tab')
      expect(
        await dialog.evaluate(
          (el) => el.contains(document.activeElement) || document.activeElement === document.body,
        ),
      ).toBe(true)
    }
    for (const viewport of [
      { width: 320, height: 900 },
      { width: 390, height: 360 },
      { width: 1440, height: 900 },
    ]) {
      const { width, height } = viewport
      await page.setViewportSize(viewport)
      await page.evaluate(() => {
        document.documentElement.style.fontSize = '200%'
      })
      const metrics = await page.locator('.modal').evaluate((el) => ({
        overflow: el.scrollWidth - el.clientWidth,
        right: el.getBoundingClientRect().right,
        font: parseFloat(getComputedStyle(el.querySelector('input')!).fontSize),
      }))
      expect(metrics.overflow).toBeLessThanOrEqual(1)
      expect(metrics.right).toBeLessThanOrEqual(width)
      expect(metrics.font).toBeGreaterThanOrEqual(32)
      await dialog.locator('button').last().scrollIntoViewIfNeeded()
      const button = await dialog.locator('button').last().boundingBox()
      expect(button!.y + button!.height).toBeLessThanOrEqual(height)
    }
    await expectTextContrast('.field-label', '.modal')
    await input.focus()
    expect(await input.evaluate((el) => getComputedStyle(el).outlineStyle)).toBe('solid')
    await page.keyboard.press('Escape')
    await expect.poll(() => dialog.count()).toBe(0)
    expect(await trigger.evaluate((el) => el === document.activeElement)).toBe(true)
    await trigger.click()
    await input.fill('Finishing pass')
    await input.press('Enter')
    await expect.poll(() => createdName).toBe('Finishing pass')
    await expect.poll(() => page.url()).toContain(task.guild_id)
    await page.locator('.app-layout .guild-name').waitFor()
  }, 30_000)

  it('wraps Discord onboarding status at 200% text size and respects reduced motion', async () => {
    await installTypographyFixture(page)
    await page.route('**/api/discord/connect', (route) =>
      route.fulfill({ json: { discord_username: 'long-discord-name'.repeat(12) } }),
    )
    // Vite proxies /auth; serve the SPA shell for this frontend-only navigation.
    await page.route('**/auth/discord/connect*', async (route) =>
      route.fulfill({ response: await route.fetch({ url: baseUrl }) }),
    )
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await page.setViewportSize({ width: 320, height: 600 })
    await page.goto(`${baseUrl}/auth/discord/connect?token=synthetic-token`)
    await page.locator('.connect-success').waitFor()
    await page.evaluate(() => {
      document.documentElement.style.fontSize = '200%'
    })
    expect(
      await page.locator('.connect-card').evaluate((el) => el.scrollWidth - el.clientWidth),
    ).toBeLessThanOrEqual(1)
    expect(
      await page.locator('.connect-icon').evaluate((el) => getComputedStyle(el).animationName),
    ).toBe('none')
    await page.locator('.connect-btn').scrollIntoViewIfNeeded()
    const button = await page.locator('.connect-btn').boundingBox()
    expect(button!.y + button!.height).toBeLessThanOrEqual(600)
    await page.goto(`${baseUrl}/auth/discord/connect`)
    await page.locator('.connect-error').waitFor()
    await expectTextContrast('.connect-error', '.connect-error')
    await page.emulateMedia({ reducedMotion: null })
  }, 30_000)

  it('keeps attachment failures and the image dialog usable at 200% text size', async () => {
    page.setDefaultTimeout(5000)
    await installTypographyFixture(page)
    const image = Buffer.from(
      'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6nxsAAAAASUVORK5CYII=',
      'base64',
    )
    const name = `${'long-screenshot-name'.repeat(12)}.png`
    let uploads = 0
    await page.route(`**/guilds/${task.guild_id}/artifacts`, async (route) => {
      uploads++
      await route.fulfill({
        status: 503,
        json: { detail: `Upload failed: ${'long-error-message'.repeat(20)}` },
      })
    })
    await page.route(`**/guilds/${task.guild_id}`, (route) =>
      route.fulfill({
        json: {
          id: task.guild_id,
          name: 'Attachment checks',
          primary_repo: task.issue_repo,
          agents: [],
          messages: [
            {
              type: 'chat',
              from: 'foreman',
              content: `[${name}](${baseUrl}/api/artifacts/finishing-image/public)`,
            },
          ],
        },
      }),
    )
    await page.route(`**/guilds/${task.guild_id}/artifacts/finishing-image`, (route) =>
      route.fulfill({
        json: {
          id: 'finishing-image',
          url: '/api/artifacts/finishing-image/public',
          contentType: 'image/png',
        },
      }),
    )
    await page.route('**/api/artifacts/finishing-image/public', (route) =>
      route.fulfill({ contentType: 'image/png', body: image }),
    )
    await page.setViewportSize({ width: 320, height: 900 })
    await page.goto(`${baseUrl}/${task.guild_id}`)
    await page
      .locator('.chat-pane input[type="file"]')
      .setInputFiles({ name, mimeType: 'image/png', buffer: image })
    await page.locator('.tray-error').waitFor()
    await page.evaluate(() => {
      document.documentElement.style.fontSize = '200%'
    })
    const metrics = await page.locator('.attachment-tray').evaluate((el) => ({
      overflow: el.scrollWidth - el.clientWidth,
      text: parseFloat(getComputedStyle(el.querySelector('.tray-error')!).fontSize),
      remove: el.querySelector('.tray-remove')!.getBoundingClientRect().width,
      retry: el.querySelector('.tray-retry')!.getBoundingClientRect().height,
    }))
    expect(metrics.overflow).toBeLessThanOrEqual(1)
    expect(metrics.text).toBeGreaterThanOrEqual(26)
    expect(metrics.remove).toBeGreaterThanOrEqual(44)
    expect(metrics.retry).toBeGreaterThanOrEqual(44)
    await expectTextContrast('.tray-error', '.chat-body')
    expect(
      await page.locator('.app-layout').evaluate((el) => el.scrollWidth - el.clientWidth),
    ).toBeLessThanOrEqual(1)
    await page.locator('.tray-retry').click()
    await expect.poll(() => uploads).toBe(2)
    await page.locator('.tray-remove').click()
    await expect.poll(() => page.locator('.tray-item').count()).toBe(0)
    await page.locator('.mobile-tab-bar').getByRole('button', { name: 'Tasks' }).click()
    await page.getByRole('button', { name: 'Show filters and search' }).click()
    expect(
      await page.locator('.sidebar').evaluate((el) => el.scrollWidth - el.clientWidth),
    ).toBeLessThanOrEqual(1)
    await page.locator('.mobile-tab-bar').getByRole('button', { name: 'Work' }).click()
    expect(
      await page.locator('.app-layout').evaluate((el) => el.scrollWidth - el.clientWidth),
    ).toBeLessThanOrEqual(1)
    await page.getByRole('button', { name: /Projects \/ Issues/ }).click()
    await page.locator('.issue-select').first().click()
    expect(
      await page.locator('.issue-viewer').evaluate((el) => el.scrollWidth - el.clientWidth),
    ).toBeLessThanOrEqual(1)
    expect(
      await page.locator('.issue-tab-content').evaluate((el) => el.clientHeight),
    ).toBeGreaterThan(0)
    await page.getByRole('button', { name: /Tasks \/ Work/ }).click()
    await page.locator('.mobile-tab-bar').getByRole('button', { name: 'Chat' }).click()
    const thumbnail = page.locator('.artifact-thumb')
    await thumbnail.focus()
    await page.keyboard.press('Enter')
    const dialog = page.locator('dialog.lightbox')
    await dialog.waitFor()
    expect(await dialog.evaluate((el) => el.matches(':modal'))).toBe(true)
    for (let index = 0; index < 4; index++) {
      await page.keyboard.press('Tab')
      expect(
        await dialog.evaluate(
          (el) => el.contains(document.activeElement) || document.activeElement === document.body,
        ),
      ).toBe(true)
    }
    expect(await page.locator('.lightbox-name').textContent()).toBe(name)
    await expect
      .poll(() =>
        page
          .locator('.lightbox-img')
          .evaluate((el) => (el instanceof HTMLImageElement ? el.naturalWidth : 0)),
      )
      .toBeGreaterThan(0)
    expect(await dialog.evaluate((el) => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(1)
    expect(
      await page.locator('.lightbox-img').evaluate((el) => el.getBoundingClientRect().height),
    ).toBeGreaterThan(0)
    expect(await page.locator('.lightbox-btn[download]').getAttribute('download')).toBe(name)
    await page.keyboard.press('Escape')
    await expect.poll(() => dialog.count()).toBe(0)
    expect(await thumbnail.evaluate((el) => el === document.activeElement)).toBe(true)
  }, 30_000)
})
