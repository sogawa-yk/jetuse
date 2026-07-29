// OCR シナリオ用のテスト画像を生成する（外部素材に依存しないため）。
// 使い方: node ops/e2e/make-assets.mjs ops/e2e/assets/ocr.png
import { chromium } from 'playwright'
const b = await chromium.launch()
const p = await b.newPage({ viewport: { width: 1000, height: 320 } })
await p.setContent(`<body style="margin:0;background:#fff;font:36px/1.6 Helvetica,Arial;color:#000;padding:40px">
<div>JetUse OCR TEST 2026</div><div>INVOICE NO 12345</div><div>合計金額 98,760 円</div></body>`)
await p.screenshot({ path: process.argv[2] })
await b.close()
