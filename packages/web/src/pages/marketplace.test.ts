import { describe, it, expect } from 'vitest'
import { filterPlugins, allTags, updateLabel, type Plugin } from './marketplace'

/** マーケットプレイス(PLG-06)の純粋ヘルパ: 検索・タグ絞り込み・版比較ラベル。 */

const faq: Plugin = {
  id: 'acme/faq',
  version: '1.2.0',
  kind: 'usecase',
  name: 'FAQ要約',
  description: 'FAQを要約する',
  tags: ['faq', 'rag'],
  installed: true,
  installed_versions: ['1.0.0'],
  update_available: true,
}
const sum: Plugin = {
  id: 'acme/summarize',
  version: '0.9.0',
  kind: 'agent',
  name: 'Summarizer',
  description: 'text agent',
  tags: ['text'],
  installed: false,
  update_available: false,
}

describe('filterPlugins', () => {
  it('returns all when query and tag are empty', () => {
    expect(filterPlugins([faq, sum], '', '')).toHaveLength(2)
  })

  it('matches id/name/description case-insensitively', () => {
    expect(filterPlugins([faq, sum], 'summ', '').map((p) => p.id)).toEqual(['acme/summarize'])
    expect(filterPlugins([faq, sum], 'FAQ', '').map((p) => p.id)).toEqual(['acme/faq'])
    expect(filterPlugins([faq, sum], '要約', '').map((p) => p.id)).toEqual(['acme/faq'])
  })

  it('filters by tag', () => {
    expect(filterPlugins([faq, sum], '', 'text').map((p) => p.id)).toEqual(['acme/summarize'])
    expect(filterPlugins([faq, sum], '', 'faq').map((p) => p.id)).toEqual(['acme/faq'])
  })

  it('combines query and tag (AND)', () => {
    expect(filterPlugins([faq, sum], 'summ', 'faq')).toHaveLength(0)
  })
})

describe('allTags', () => {
  it('returns a sorted unique tag set', () => {
    expect(allTags([faq, sum])).toEqual(['faq', 'rag', 'text'])
  })

  it('handles plugins without tags', () => {
    expect(allTags([{ ...sum, tags: undefined }])).toEqual([])
  })
})

describe('updateLabel', () => {
  it('shows installed→latest when an update is available', () => {
    expect(updateLabel(faq)).toBe('v1.0.0 → v1.2.0')
  })

  it('returns null when no update', () => {
    expect(updateLabel(sum)).toBeNull()
    expect(updateLabel({ ...faq, update_available: false })).toBeNull()
  })

  it('returns null when no installed version is known', () => {
    expect(updateLabel({ ...faq, installed_versions: [] })).toBeNull()
  })
})
