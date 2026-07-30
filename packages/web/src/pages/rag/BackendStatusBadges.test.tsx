import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { PrefsProvider } from '../../prefs'
import { BackendStatusBadges } from './BackendStatusBadges'
import type { BackendStatus, RagBackend } from './capabilityCatalog'

/** 取り込み状況バッジ(「取り込めたか」)。RAGM-03 で adb も出るようにした。
 *  能力差パネル(「何ができるか」)とは別物であることを、意味の違うテストで固定する。 */

const renderBadges = (backends: Partial<Record<RagBackend, BackendStatus>>) =>
  render(
    <PrefsProvider>
      <BackendStatusBadges backends={backends} />
    </PrefsProvider>,
  )

describe('BackendStatusBadges', () => {
  it('shows adb ingestion state alongside the existing backends', () => {
    renderBadges({
      vector_store: 'indexed', adb: 'indexed', select_ai: 'pending', opensearch: 'disabled',
    })
    expect(screen.getByText('✓ ADB')).toBeInTheDocument()
    expect(screen.getByText('✓ VS')).toBeInTheDocument()
    expect(screen.getByText('⏳ SAI')).toBeInTheDocument()
    expect(screen.getByText('– OS')).toBeInTheDocument()
  })

  it.each<[BackendStatus, string, string]>([
    ['indexed', '✓ ADB', '取り込み済み'],
    ['pending', '⏳ ADB', '取り込み中（同期待ち）'],
    ['error', '! ADB', '取り込み失敗'],
    ['disabled', '– ADB', '無効'],
  ])('renders adb %s as "%s"', (status, mark, title) => {
    renderBadges({ adb: status })
    const badge = screen.getByText(mark)
    expect(badge.getAttribute('title')).toBe(`Oracle AI Database（自前索引）: ${title}`)
  })

  it('omits backends the API response does not mention (forward compatible)', () => {
    renderBadges({ vector_store: 'indexed' })
    expect(screen.queryByText(/ADB/)).not.toBeInTheDocument()
    expect(screen.queryByText(/SAI/)).not.toBeInTheDocument()
  })
})
