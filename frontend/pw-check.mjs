import { chromium } from 'playwright'

const browser = await chromium.launch({ args: ['--no-sandbox'] })
const page = await browser.newPage({ viewport: { width: 1000, height: 800 } })
const errors = []
page.on('console', (msg) => {
  if (msg.type() === 'error') errors.push(msg.text())
})
page.on('pageerror', (e) => errors.push(String(e)))

await page.goto('http://localhost:5173/harness.html')
await page.waitForSelector('text=Spawn Worker')
await page.screenshot({ path: '/tmp/spawn-1-general.png' })

// Check General tab content
const generalText = await page.textContent('.settings-content')
console.log('GENERAL_TAB_HAS_REPOS:', generalText.includes('acme'))

// Switch to Tools tab
await page.click('.settings-tab:has-text("Tools")')
await page.waitForSelector('text=Agents')
await page.screenshot({ path: '/tmp/spawn-2-tools.png' })
const toolsText = await page.textContent('.settings-content')
console.log('TOOLS_TAB_HAS_CLAUDE:', toolsText.includes('claude'))

// Switch to Environment tab
await page.click('.settings-tab:has-text("Environment")')
await page.waitForSelector('text=ANTHROPIC_API_KEY')
await page.screenshot({ path: '/tmp/spawn-3-environment.png' })
const envText = await page.textContent('.settings-content')
console.log('ENV_TAB_HAS_CRED:', envText.includes('ANTHROPIC_API_KEY'))

// Footer + Launch button should be visible regardless of tab
const launchVisible = await page.isVisible('.spawn-launch-btn')
console.log('LAUNCH_BTN_VISIBLE:', launchVisible)

// Close button works
await page.click('.settings-close-btn')
await page.waitForTimeout(200)

console.log('CONSOLE_ERRORS:', JSON.stringify(errors))

await browser.close()
