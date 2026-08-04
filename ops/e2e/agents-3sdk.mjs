// PORT-03: 3SDK のホスト型エージェントを実ブラウザ経由の API で実行する。
// - 各SDKでエージェントを作成し、ツール実行を伴う質問を投げる
// - min_replica=0 からの初回実行（コールドスタート）の所要時間を計測する
import { chromium } from 'playwright'
import fs from 'node:fs'

const APP = process.env.APP_URL.replace(/\/$/, '')
const OUT = process.env.OUT_DIR || './shots-agents'
fs.mkdirSync(OUT, { recursive: true })
const results = []
const rec = (name, ok, detail = '') => {
  results.push({ name, ok, detail: String(detail).replace(/\s+/g, ' ').slice(0, 300) })
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ' — ' + String(detail).replace(/\s+/g, ' ').slice(0, 200) : ''}`)
}

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
  return { status: r.status, body: (await r.text()).slice(0, 6000) }
}, { path, init })

const SDKS = [
  ['openai_agents', 'OpenAI Agents SDK'],
  ['langgraph', 'LangGraph'],
  ['adk', 'ADK'],
]
const timings = {}
for (const [sdk, label] of SDKS) {
  const mk = await call('/api/agents', {
    method: 'POST',
    body: JSON.stringify({
      name: `port03-${sdk}`, instructions: '日本語で簡潔に答える。時刻を聞かれたら必ず get_current_time ツールを使う。',
      model: 'gpt-oss-120b', framework: sdk, enabled_tools: ['get_current_time'],
    }),
  })
  let id = ''
  try { id = JSON.parse(mk.body).id } catch {}
  rec(`${label}: エージェント作成`, mk.status === 200 && !!id, `${mk.status} ${mk.body.slice(0, 120)}`)
  if (!id) continue

  const t0 = Date.now()
  const run = await call('/api/chat/stream', {
    method: 'POST',
    body: JSON.stringify({ model: 'gpt-oss-120b', agent_id: id, messages: [{ role: 'user', content: '今日は何曜日ですか。ツールで調べて答えてください。' }] }),
  })
  const secs = ((Date.now() - t0) / 1000).toFixed(1)
  timings[sdk] = Number(secs)
  const notConfigured = /配備されていません|not configured/.test(run.body)
  const usedTool = /get_current_time/.test(run.body)
  const hasDelta = /"delta"/.test(run.body)
  const hasError = /"error"/.test(run.body)
  rec(`${label}: 「未設定」エラーが出ない`, !notConfigured, run.body.slice(0, 160))
  rec(`${label}: ツール実行を伴う応答（${secs}s）`, usedTool && hasDelta && !hasError, run.body.slice(0, 220))
}

fs.writeFileSync(`${OUT}/timings.json`, JSON.stringify(timings, null, 2))
// 出力名はハーネスごとに分ける。同じ OUT_DIR で public-deploy.mjs と続けて走らせるのが
// 通常運用なので、共通の results.json にすると**先に走ったほうの証跡を黙って上書きする**
// （PUBLIC-IAM-02 で実際に 39項目の結果を失った）。
fs.writeFileSync(`${OUT}/agents-3sdk-results.json`, JSON.stringify(results, null, 2))
console.log('\n--- コールドスタート含む初回実行時間(秒) ---')
console.log(JSON.stringify(timings))
const ok = results.filter((r) => r.ok).length
console.log(`\n=== ${ok}/${results.length} PASS ===`)
await browser.close()
process.exit(ok === results.length ? 0 : 1)
