import { describe, it, expect } from 'vitest'
import { UPLOAD_ACCEPT, UPLOAD_EXTENSIONS } from './uploadFormats'
import { ja } from '../../i18n/dict.ja'
import { en } from '../../i18n/dict.en'

/** RAGM03-002: サーバが受け付ける形式を画面から選べること。
 *  xlsx はチャンクごとのセル範囲出典（このタスクの主役）の入力形式なので、
 *  ここが欠けると「能力表には出るのに試せない」状態になる。 */
describe('upload formats', () => {
  it('accepts every extension the API allows (rag.ALLOWED_EXTENSIONS)', () => {
    expect([...UPLOAD_EXTENSIONS].sort()).toEqual(
      ['.jpeg', '.jpg', '.md', '.pdf', '.png', '.txt', '.xlsx'],
    )
    expect(UPLOAD_ACCEPT).toContain('.xlsx')
  })

  it('tells the user that Excel is supported (ja / en)', () => {
    expect(ja['rag.supported']).toMatch(/xlsx/)
    expect(en['rag.supported']).toMatch(/xlsx/)
  })

  /** PREP-03: スキャン PDF と画像は OCR を通して取り込む。選べないと試せない。 */
  it('offers images as well (they are OCRed on ingest)', () => {
    for (const ext of ['.png', '.jpg', '.jpeg']) expect(UPLOAD_ACCEPT).toContain(ext)
    expect(ja['rag.supported']).toMatch(/画像/)
    expect(en['rag.supported']).toMatch(/image/i)
  })

  /** 画像はサーバ側の上限が別（8MB = OCR の inline 上限）。案内と実際がずれると
   *  「選べたのに 422」になる。 */
  it('states the separate image size limit (ja / en)', () => {
    expect(ja['rag.supported']).toMatch(/20MB/)
    expect(ja['rag.supported']).toMatch(/8MB/)
    expect(en['rag.supported']).toMatch(/20MB/)
    expect(en['rag.supported']).toMatch(/8MB/)
  })
})
