import { describe, expect, it } from 'vitest'
import { checklist, deriveStep, type Session } from './state'

/** デモビルダー(SP3-05)純ロジック: サーバ状態→ステップ導出・必須項目チェックリスト(specs/19 §7・§2.2) */

const base: Session = {
  id: 's1',
  status: 'hearing',
  transcript: [],
  requirements: null,
  plan: null,
  demo_id: null,
  demo_status: null,
  created_at: null,
  updated_at: null,
}

const plan = {
  plan_version: 1,
  title: 'T',
  description: 'D',
  capabilities: ['chat'],
  screens: [],
  data: {},
}

describe('deriveStep', () => {
  it('returns 1 for no session', () => {
    expect(deriveStep(null)).toBe(1)
  })

  it('returns 1 while hearing', () => {
    expect(deriveStep(base)).toBe(1)
  })

  it('returns 2 when designed with a plan', () => {
    expect(deriveStep({ ...base, status: 'designed', plan })).toBe(2)
  })

  it('returns 1 when designed but plan is missing (defensive)', () => {
    expect(deriveStep({ ...base, status: 'designed' })).toBe(1)
  })

  it('returns 3 while provisioning', () => {
    expect(
      deriveStep({ ...base, status: 'designed', plan, demo_id: 'd1', demo_status: 'provisioning' }),
    ).toBe(3)
  })

  it('returns 3 when failed (failed view lives in step 3)', () => {
    expect(
      deriveStep({ ...base, status: 'designed', plan, demo_id: 'd1', demo_status: 'failed' }),
    ).toBe(3)
  })

  it('returns 4 when ready', () => {
    expect(
      deriveStep({ ...base, status: 'designed', plan, demo_id: 'd1', demo_status: 'ready' }),
    ).toBe(4)
  })

  it('returns 3 when demo status is unknown (poll resolves it)', () => {
    expect(deriveStep({ ...base, status: 'designed', plan, demo_id: 'd1' })).toBe(3)
  })
})

describe('checklist', () => {
  it('is all-unmet for null requirements', () => {
    expect(checklist(null)).toEqual([
      { key: 'industry', ok: false },
      { key: 'use_case', ok: false },
      { key: 'data', ok: false },
    ])
  })

  it('marks filled required fields', () => {
    const items = checklist({ industry: '製造', use_case: null, data_profile: null })
    expect(items).toEqual([
      { key: 'industry', ok: true },
      { key: 'use_case', ok: false },
      { key: 'data', ok: false },
    ])
  })

  it('accepts either documents or tables for data', () => {
    expect(checklist({ data_profile: { documents: '保全マニュアル' } })[2].ok).toBe(true)
    expect(checklist({ data_profile: { tables: '故障履歴' } })[2].ok).toBe(true)
  })

  it('does not accept whitespace-only values', () => {
    const items = checklist({ industry: '  ', data_profile: { documents: ' ' } })
    expect(items[0].ok).toBe(false)
    expect(items[2].ok).toBe(false)
  })
})
