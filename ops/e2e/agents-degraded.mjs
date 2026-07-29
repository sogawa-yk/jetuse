// PORT-03 シナリオ4: ホスト型エージェント無効化構成での縮退を確認する。
// - /api/health の capabilities.agents が理由付きの unavailable になること
// - 実行時に内部識別子ではなく「なぜ使えないか・どうすれば使えるか」が返ること
import { chromium } from 'playwright'
import fs from 'node:fs'
const APP = process.env.APP_URL.replace(/\/$/, '')
const OUT = process.env.OUT_DIR || './shots-degraded'
fs.mkdirSync(OUT, { recursive: true })
const results = []
const rec = (n, ok, d = '') => { results.push({ name: n, ok, detail: String(d).replace(/\s+/g,' ').slice(0,300) }); console.log(`${ok?'PASS':'FAIL'}  ${n} — ${String(d).replace(/\s+/g,' ').slice(0,200)}`) }

const browser = await chromium.launch({ headless: true })
const page = await (await browser.newContext({ viewport: { width: 1440, height: 950 } })).newPage()
await page.goto(APP + '/', { waitUntil: 'domcontentloaded', timeout: 90_000 })
await page.waitForTimeout(2500)
if (/identity\.oraclecloud\.com/.test(page.url())) {
  await page.locator('#idcs-signin-basic-signin-form-username').fill(process.env.DEMO_USER, { timeout: 30_000 })
  await page.locator('input[type=password]').first().fill(process.env.DEMO_PASS)
  await page.getByRole('button', { name: 'Sign In' }).click()
  await page.waitForURL((u) => new URL(u).host === new URL(APP).host, { timeout: 60_000 }).catch(() => page.waitForTimeout(8000))
  await page.waitForFunction(() => Object.keys(sessionStorage).concat(Object.keys(localStorage)).some((k) => /oidc|token/i.test(k)), { timeout: 30_000 }).catch(() => {})
  await page.waitForTimeout(3000)
}
const call = (path, init) => page.evaluate(async ({ path, init }) => {
  const raw = Object.keys(sessionStorage).concat(Object.keys(localStorage)).find((k) => /oidc|token/i.test(k))
  const store = sessionStorage.getItem(raw) || localStorage.getItem(raw)
  let tok = ''
  try { tok = JSON.parse(store).access_token || JSON.parse(store).id_token || '' } catch { tok = store || '' }
  const h = { Authorization: `Bearer ${tok}` }
  if (init?.body) h['Content-Type'] = 'application/json'
  const r = await fetch(path, { method: init?.method || 'GET', headers: h, body: init?.body })
  return { status: r.status, body: (await r.text()).slice(0, 4000) }
}, { path, init })

const h = JSON.parse((await call('/api/health')).body)
const ag = h.capabilities?.agents || {}
rec('capabilities.agents が unavailable', ag.status === 'unavailable', JSON.stringify(ag).slice(0, 240))
rec('理由（ヒント）が付く', typeof ag.hint === 'string' && ag.hint.length > 10, ag.hint || '(none)')
rec('内部識別子を露出しない', !/missing=|app_ocid|HOSTED_AGENT_/.test(ag.hint || ''), ag.hint || '')
rec('他機能は生きている（chat が ok）', h.capabilities?.chat?.status === 'ok', h.capabilities?.chat?.status)

const mk = await call('/api/agents', { method: 'POST', body: JSON.stringify({ name: 'port03-degraded', instructions: 'x', model: 'gpt-oss-120b', framework: 'langgraph', enabled_tools: [] }) })
let id = ''
try { id = JSON.parse(mk.body).id } catch {}
if (id) {
  const run = await call('/api/chat/stream', { method: 'POST', body: JSON.stringify({ model: 'gpt-oss-120b', agent_id: id, messages: [{ role: 'user', content: 'こんにちは' }] }) })
  rec('実行時に理由が返る', /配備されていません/.test(run.body), run.body.slice(0, 240))
  rec('生の内部エラー文字列を返さない', !/agent container not configured|missing=/.test(run.body), run.body.slice(0, 200))
}
fs.writeFileSync(`${OUT}/results.json`, JSON.stringify(results, null, 2))
const ok = results.filter(r => r.ok).length
console.log(`\n=== ${ok}/${results.length} PASS ===`)
await browser.close()
process.exit(ok === results.length ? 0 : 1)
