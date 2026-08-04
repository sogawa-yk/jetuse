// 共通: ログイン済み page を返す
import { chromium } from 'playwright'
export async function login(APP, USER, PASS, opts = {}) {
  const browser = await chromium.launch({ headless: opts.headless !== false })
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 950 } })
  const page = await ctx.newPage()
  await page.goto(APP, { waitUntil: 'domcontentloaded', timeout: 90_000 })
  await page.waitForTimeout(2500)
  if (/identity\.oraclecloud\.com/.test(page.url())) {
    await page.locator('#idcs-signin-basic-signin-form-username').fill(USER, { timeout: 30_000 })
    await page.locator('input[type=password]').first().fill(PASS)
    await page.getByRole('button', { name: 'Sign In' }).click()
    await page.waitForURL((u) => u.host === new URL(APP).host, { timeout: 90_000 })
  }
  await page.waitForTimeout(4000)
  return { browser, ctx, page }
}
export async function apiGet(page, path) {
  return page.evaluate(async (p) => {
    const k = Object.keys(sessionStorage).find((x) => x.startsWith('oidc.user:'))
    const tok = k ? JSON.parse(sessionStorage.getItem(k)).access_token : null
    const r = await fetch(p, { headers: tok ? { Authorization: 'Bearer ' + tok } : {} })
    const t = await r.text()
    try { return { status: r.status, json: JSON.parse(t) } } catch { return { status: r.status, text: t.slice(0, 2000) } }
  }, path)
}
