// JetUse Public版 ワンクリックデプロイ 受け入れ E2E（FIX-58 で作成・38項目）。
// 使い方と注意は ops/e2e/README.md。証跡へ残す前に URL/OCID をマスクすること。
// 実 OCI デプロイに対し Chromium で操作/API 実行し、機能ごとに合否を出す。
import { chromium } from 'playwright'
import fs from 'node:fs'

const APP = process.env.APP_URL.replace(/\/$/, '')
const USER = process.env.DEMO_USER
const PASS = process.env.DEMO_PASS
const OUT = process.env.OUT_DIR || './shots-final'
const ASSETS = process.env.ASSETS || 'ops/e2e/assets'
fs.mkdirSync(OUT, { recursive: true })

const results = []
const rec = (name, ok, detail = '') => {
  results.push({ name, ok, detail: String(detail).replace(/\s+/g, ' ').slice(0, 260) })
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ' — ' + String(detail).replace(/\s+/g, ' ').slice(0, 180) : ''}`)
}

const browser = await chromium.launch({
  headless: true,
  args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'],
})
const ctx = await browser.newContext({ viewport: { width: 1440, height: 950 }, permissions: ['microphone'] })
const page = await ctx.newPage()
const httpErrors = []
page.on('response', (r) => r.status() >= 400 && httpErrors.push(`${r.status()} ${r.request().method()} ${r.url().replace(APP, '')}`))
const shot = (n) => page.screenshot({ path: `${OUT}/${n}.png` }).catch(() => {})

// ===== 1. ログイン(スタック出力のパスワードでそのまま入れること) =====
await page.goto(APP + '/', { waitUntil: 'domcontentloaded', timeout: 90_000 })
await page.waitForTimeout(2500)
const atSignin = /identity\.oraclecloud\.com/.test(page.url())
if (atSignin) {
  await page.locator('#idcs-signin-basic-signin-form-username').fill(USER, { timeout: 30_000 })
  await page.locator('input[type=password]').first().fill(PASS)
  await page.getByRole('button', { name: 'Sign In' }).click()
  // 固定待ちだとリダイレクト（IDCS → authorize → アプリ）が終わる前に判定してしまい、
  // 以降のAPIが軒並み401になる（2026-07-29 PORT-03 のE2Eで実際に踏んだ）。
  // アプリのホストに戻るまで待ち、届かなければ従来の固定待ちにフォールバックする。
  await page
    .waitForURL((u) => new URL(u).host === new URL(APP).host, { timeout: 60_000 })
    .catch(() => page.waitForTimeout(8000))
  // URL が戻った時点ではまだ SPA が認可コードをトークンに交換し終えていないことがあり、
  // 直後の API 呼び出しが `invalid token: DecodeError` で落ちる。交換完了まで待つ。
  await page
    .waitForFunction(() => Object.keys(sessionStorage).concat(Object.keys(localStorage))
      .some((k) => /oidc|token/i.test(k)), { timeout: 30_000 })
    .catch(() => {})
  await page.waitForTimeout(3000)
}
await shot('01-after-login')
const forcedPwChange = /pwdmustchange|pwdexpired/.test(page.url())
rec('デモユーザーがパスワード変更を強制されない', !forcedPwChange, page.url())
await page.waitForTimeout(3000)
rec('ログイン→アプリ表示', new URL(page.url()).host === new URL(APP).host, page.url())

const call = (path, init) => page.evaluate(async ({ path, init }) => {
  const k = Object.keys(sessionStorage).find((x) => x.startsWith('oidc.user:'))
  const tok = k ? JSON.parse(sessionStorage.getItem(k)).access_token : null
  const h = { Authorization: 'Bearer ' + tok }
  if (init?.body) h['Content-Type'] = 'application/json'
  const r = await fetch(path, { method: init?.method || 'GET', headers: h, body: init?.body })
  return { status: r.status, body: (await r.text()).slice(0, 4000) }
}, { path, init })
const json = async (p) => { const r = await call(p); try { return JSON.parse(r.body) } catch { return { _status: r.status, _raw: r.body } } }

// ===== 2. 自己診断 =====
const health = await json('/api/health')
rec('/api/health 全体が ok', health.ok === true, JSON.stringify(health).slice(0, 200))
for (const [k, v] of Object.entries(health.capabilities || {})) {
  rec(`capability: ${k}`, v.status === 'ok', `${v.status} ${v.hint || JSON.stringify(v).slice(0, 120)}`)
}
const ragHealth = await json('/api/rag/health')
rec('/api/rag/health 3点すべて ok', ragHealth.ok === true, JSON.stringify(ragHealth.checks))

// ===== 3. チャット(既定モデル。フォールバック通知が出ないこと) =====
{
  const r = await call('/api/chat/stream', { method: 'POST', body: JSON.stringify({ model: 'gpt-oss-120b', messages: [{ role: 'user', content: 'OCIの利点を1つ、20文字以内で' }] }) })
  const hasErr = /"error"/.test(r.body)
  const hasNotice = /"notice"/.test(r.body)
  const hasDelta = /"delta"/.test(r.body)
  rec('チャット: 既定モデル(gpt-oss-120b)が応答', hasDelta && !hasErr, r.body.replace(/\n/g, '').slice(0, 200))
  rec('チャット: 既定モデルのフォールバック通知が出ない', !hasNotice, hasNotice ? 'notice あり' : '')
}
// 全モデル
for (const m of ['llama-3.3-70b', 'gemini-2.5-pro', 'gemini-2.5-flash', 'llama-3.2-90b-vision']) {
  const r = await call('/api/chat/stream', { method: 'POST', body: JSON.stringify({ model: m, messages: [{ role: 'user', content: 'say OK' }] }) })
  rec(`チャット: モデル ${m}`, /"delta"/.test(r.body) && !/"error"/.test(r.body), r.body.replace(/\n/g, '').slice(0, 120))
}

// ===== 4. 会話メモリ(Conversations API) =====
{
  const r = await call('/api/conversations', { method: 'POST', body: JSON.stringify({ title: 'e2e', model: 'gpt-oss-120b' }) })
  rec('会話メモリ: 会話作成(OCI Conversations)', r.status < 400 && !/null/.test((JSON.parse(r.body || '{}') || {}).oci_conversation_id ?? 'x'), `${r.status} ${r.body.slice(0, 200)}`)
}

// ===== 5. RAG(UI からアップロード → 引用付き回答) =====
{
  await page.goto(`${APP}/#/rag`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3000)
  const doc = `${OUT}/keihi.txt`
  fs.writeFileSync(doc, '社内経費規程(抜粋)\n第3条 交通費の1回あたりの上限は 12,000 円とする。\n第5条 経費精算の締め日は毎月25日とする。\n第7条 会食費の1人あたり上限は 6,000 円とする。\n')
  const before = httpErrors.length
  await page.locator('input[type=file]').first().setInputFiles(doc, { timeout: 20_000 })
  let uploaded = false
  for (let i = 0; i < 50; i++) {
    await page.waitForTimeout(3000)
    if (/keihi/.test(await page.locator('#root').innerText())) { uploaded = true; break }
  }
  await shot('05-rag-uploaded')
  const gwErrs = httpErrors.slice(before).filter((e) => e.includes('/api/rag'))
  rec('RAG: 初回アップロードがゲートウェイ504にならない', gwErrs.length === 0, gwErrs.join(' | '))
  rec('RAG: 文書取り込み完了', uploaded)
  if (uploaded) {
    // 索引化(vector_store=indexed)まで待ってから質問する
    let indexed = false
    for (let i = 0; i < 40; i++) {
      const f = await json('/api/rag/files')
      if ((f.files || []).some((x) => x.status === 'completed' && x.backends?.vector_store === 'indexed')) { indexed = true; break }
      await page.waitForTimeout(5000)
    }
    rec('RAG: Vector Store 索引化完了', indexed)
    const r = await call('/api/chat/stream', { method: 'POST', body: JSON.stringify({ model: 'gpt-oss-120b', rag: true, messages: [{ role: 'user', content: '経費精算の締め日は?' }] }) })
    const text = (r.body.match(/"delta": "([^"]*)"/g) || []).join('')
    rec('RAG: 文書に基づく回答(引用付き)', /25/.test(text) && /citations/.test(r.body), text.slice(0, 160))
  }
}

// ===== 6. DBチャット(UI: SQL生成 → 実行) =====
{
  await page.goto(`${APP}/#/dbchat`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3500)
  await page.locator('textarea').first().fill('2001年の販売チャネル別売上を教えて')
  await page.getByRole('button', { name: /SQL生成|Generate/ }).first().click()
  // 「生成されたSQL（編集できます）」のtextareaに実際にSELECTが入るまで待つ
  const sqlBox = page.locator('textarea').nth(1)
  let sql = false
  for (let i = 0; i < 40; i++) {
    await page.waitForTimeout(2500)
    if (/SELECT/i.test((await sqlBox.inputValue().catch(() => '')) || '')) { sql = true; break }
  }
  await shot('06-dbchat-sql')
  rec('DBチャット: SQL生成', sql, ((await sqlBox.inputValue().catch(() => '')) || '').slice(0, 120))
  if (sql) {
    await page.getByRole('button', { name: /実行|Run/ }).first().click()
    let rows = false
    for (let i = 0; i < 25; i++) {
      await page.waitForTimeout(2000)
      if (/Internet|Direct Sales|Partners|Tele/i.test(await page.locator('#root').innerText())) { rows = true; break }
    }
    await shot('06-dbchat-rows')
    rec('DBチャット: SQL実行と結果表示', rows)
  }
}

// ===== 7. OCR =====
{
  await page.goto(`${APP}/#/ocr`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3000)
  await page.locator('input[type=file]').first().setInputFiles(`${ASSETS}/ocr.png`, { timeout: 20_000 })
  await page.waitForTimeout(1500)
  const runOcr = page.getByRole('button', { name: /OCR実行|Run OCR/ }).first()
  if (await runOcr.count()) await runOcr.click()
  let ok = false
  for (let i = 0; i < 40; i++) {
    await page.waitForTimeout(3000)
    if (/12345|INVOICE|98,760/i.test(await page.locator('#root').innerText())) { ok = true; break }
  }
  await shot('07-ocr')
  rec('OCR: 画像から文字抽出', ok, (await page.locator('#root').innerText()).slice(-200))
}

// ===== 8. 議事録(音声アップロード→文字起こしジョブ) =====
{
  await page.goto(`${APP}/#/minutes`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3000)
  const before = httpErrors.length
  const fi = page.locator('input[type=file]').first()
  if (await fi.count()) {
    await fi.setInputFiles(`${ASSETS}/tone.wav`, { timeout: 20_000 })
    await page.waitForTimeout(15000)
  }
  await shot('08-minutes')
  const errs = httpErrors.slice(before).filter((e) => e.includes('/api/minutes'))
  rec('議事録: 音声登録がエラーにならない', errs.length === 0, errs.join(' | '))
}

// ===== 9. 音声(STT セッション) / TTS / 翻訳 =====
{
  const r = await call('/api/stt/sessions', { method: 'POST', body: JSON.stringify({ language: 'ja' }) })
  rec('リアルタイムSTT: セッション作成', r.status < 400, `${r.status} ${r.body.slice(0, 120)}`)
}
{
  const r = await call('/api/tts', { method: 'POST', body: JSON.stringify({ text: 'テスト' }) })
  rec('TTS: 音声合成', r.status < 400, `${r.status} ${r.body.slice(0, 150)}`)
  const h2 = await json('/api/health')
  const t = h2.capabilities?.tts || {}
  rec('TTS: health が実合成の結果を反映(verified)', t.verified === true, JSON.stringify(t))
}
{
  const r = await call('/api/translate', { method: 'POST', body: JSON.stringify({ text: 'こんにちは', target: 'en' }) })
  rec('翻訳', r.status < 400, `${r.status} ${r.body.slice(0, 150)}`)
}

// ===== 10. エージェント =====
{
  const r = await call('/api/agents')
  rec('エージェント一覧', r.status < 400, `${r.status} ${r.body.slice(0, 120)}`)
}

// ===== 11. 管理ダッシュボード =====
{
  await page.goto(`${APP}/#/admin`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(4000)
  await shot('11-admin')
  const r = await call('/api/admin/usage?days=30')
  rec('管理ダッシュボード: demoユーザーが閲覧できる', r.status === 200, `${r.status} ${r.body.slice(0, 150)}`)
}

// ===== 12. 各ページの描画 =====
for (const [hash, name] of [['', 'ホーム'], ['chat', 'チャット'], ['realtime', 'リアルタイム翻訳'], ['voicechat', '音声チャット'], ['video', '映像分析'], ['builder', 'ビルダー'], ['settings', '設定']]) {
  await page.goto(`${APP}/#/${hash}`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  const t = await page.locator('#root').innerText().catch(() => '')
  rec(`ページ描画: ${name}`, t.length > 50)
}

fs.writeFileSync(`${OUT}/http-errors.txt`, httpErrors.join('\n'))
fs.writeFileSync(`${OUT}/results.json`, JSON.stringify(results, null, 2))
console.log('\n--- HTTP >=400 (unique) ---\n' + [...new Set(httpErrors)].slice(0, 40).join('\n'))
const failed = results.filter((r) => !r.ok)
console.log(`\n=== ${results.length - failed.length}/${results.length} PASS ===`)
if (failed.length) console.log('FAILED: ' + failed.map((f) => f.name).join(' / '))
await browser.close()
