import { describe, it, expect } from 'vitest'
import { hasSendableFile, isSendableWith } from './sendReadiness'

/** RAGM-04: 送信可否は「選択中のバックエンドの取り込み状態」で決まる。
 *  RAGM03-005（ADB を選んでもマネージド側の完了で送信可に見えた）を固定する。 */

const managedOnly = {
  status: 'completed' as const,
  backends: { vector_store: 'indexed', adb: 'pending', select_ai: 'pending',
              opensearch: 'disabled' } as const,
}

describe('sendReadiness', () => {
  it('does not allow sending on adb while adb has not ingested the file', () => {
    expect(hasSendableFile([managedOnly], 'adb')).toBe(false)
    expect(hasSendableFile([managedOnly], 'vector_store')).toBe(true)
  })

  it.each(['pending', 'error', 'disabled'] as const)(
    'treats adb %s as not sendable', (st) => {
      expect(isSendableWith({ status: 'completed', backends: { adb: st } }, 'adb')).toBe(false)
    },
  )

  it('allows sending once the selected backend has indexed the file', () => {
    const f = { status: 'processing' as const, backends: { vector_store: 'pending',
                adb: 'indexed' } as const }
    expect(hasSendableFile([f], 'adb')).toBe(true)
    expect(hasSendableFile([f], 'vector_store')).toBe(false)
  })

  it('is per-backend across a mixed file list', () => {
    const files = [
      { status: 'completed' as const, backends: { vector_store: 'indexed', adb: 'error' } as const },
      { status: 'processing' as const, backends: { vector_store: 'pending', adb: 'indexed' } as const },
    ]
    expect(hasSendableFile(files, 'vector_store')).toBe(true)
    expect(hasSendableFile(files, 'adb')).toBe(true)
    expect(hasSendableFile(files, 'select_ai')).toBe(false)
  })

  it('falls back to the managed status only for vector_store (forward compatible)', () => {
    const legacy = { status: 'completed' as const }
    expect(isSendableWith(legacy, 'vector_store')).toBe(true)
    expect(isSendableWith(legacy, 'adb')).toBe(false)
    expect(isSendableWith({ status: 'failed' }, 'vector_store')).toBe(false)
  })

  it('is false for an empty file list', () => {
    expect(hasSendableFile([], 'vector_store')).toBe(false)
  })
})
