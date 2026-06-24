import { describe, it, expect } from 'vitest'
import { ja } from './dict.ja'
import { en } from './dict.en'

/** prefs.tsx の i18n 辞書分割(review-validation.md §7)。
 *  ja/en のキー集合が完全一致していること(片方欠落でUIにキー文字列が露出する)を保証する。 */
describe('i18n dict', () => {
  it('ja and en have identical key sets and counts', () => {
    const jk = Object.keys(ja).sort()
    const ek = Object.keys(en).sort()
    expect(ek).toEqual(jk)
    expect(ek.length).toBe(jk.length)
  })

  it('has no empty values', () => {
    for (const [k, v] of Object.entries(ja)) expect(v, `ja[${k}]`).not.toBe('')
    for (const [k, v] of Object.entries(en)) expect(v, `en[${k}]`).not.toBe('')
  })
})
