import { describe, it, expect } from 'vitest'
import { UPLOAD_ACCEPT, UPLOAD_EXTENSIONS } from './uploadFormats'
import { ja } from '../../i18n/dict.ja'
import { en } from '../../i18n/dict.en'

/** RAGM03-002: サーバが受け付ける形式を画面から選べること。
 *  xlsx はチャンクごとのセル範囲出典（このタスクの主役）の入力形式なので、
 *  ここが欠けると「能力表には出るのに試せない」状態になる。 */
describe('upload formats', () => {
  it('accepts every extension the API allows (rag.ALLOWED_EXTENSIONS)', () => {
    expect([...UPLOAD_EXTENSIONS].sort()).toEqual(['.md', '.pdf', '.txt', '.xlsx'])
    expect(UPLOAD_ACCEPT).toContain('.xlsx')
  })

  it('tells the user that Excel is supported (ja / en)', () => {
    expect(ja['rag.supported']).toMatch(/xlsx/)
    expect(en['rag.supported']).toMatch(/xlsx/)
  })
})
