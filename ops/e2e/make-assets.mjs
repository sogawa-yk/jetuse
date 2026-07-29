// E2E シナリオ用のテスト素材を生成する（外部素材に依存しないため）。
// 使い方: node ops/e2e/make-assets.mjs ops/e2e/assets/ocr.png
//   OCR 用の画像に加えて、音声シナリオ用の tone.wav を同じディレクトリへ書き出す
//   （tone.wav が無いと public-deploy.mjs が音声テストで異常終了する — PORT-03 で判明）。
import { chromium } from 'playwright'
const b = await chromium.launch()
const p = await b.newPage({ viewport: { width: 1000, height: 320 } })
await p.setContent(`<body style="margin:0;background:#fff;font:36px/1.6 Helvetica,Arial;color:#000;padding:40px">
<div>JetUse OCR TEST 2026</div><div>INVOICE NO 12345</div><div>合計金額 98,760 円</div></body>`)
await p.screenshot({ path: process.argv[2] })
await b.close()

// 音声アップロード用の最小 WAV（16bit PCM / 16kHz / モノラル / 440Hz 1.5秒）。
import fs from 'node:fs'
import path from 'node:path'
const wavPath = path.join(path.dirname(process.argv[2]), 'tone.wav')
const rate = 16000, secs = 1.5, n = Math.floor(rate * secs)
const data = Buffer.alloc(n * 2)
for (let i = 0; i < n; i++) {
  data.writeInt16LE(Math.round(Math.sin((2 * Math.PI * 440 * i) / rate) * 12000), i * 2)
}
const head = Buffer.alloc(44)
head.write('RIFF', 0); head.writeUInt32LE(36 + data.length, 4); head.write('WAVE', 8)
head.write('fmt ', 12); head.writeUInt32LE(16, 16); head.writeUInt16LE(1, 20)
head.writeUInt16LE(1, 22); head.writeUInt32LE(rate, 24); head.writeUInt32LE(rate * 2, 28)
head.writeUInt16LE(2, 32); head.writeUInt16LE(16, 34)
head.write('data', 36); head.writeUInt32LE(data.length, 40)
fs.writeFileSync(wavPath, Buffer.concat([head, data]))
console.log('wrote', process.argv[2], 'and', wavPath)
