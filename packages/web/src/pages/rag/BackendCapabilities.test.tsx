import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { PrefsProvider } from '../../prefs'
import { BackendCapabilityPanel } from './BackendCapabilities'
import {
  pickRagBackendCapabilities,
  type RagBackendCapabilities,
} from './capabilityCatalog'

/** RAGM-03: バックエンドを切り替えると「何ができるか」の表示が変わること、
 *  未実証の能力が「使える」として出ないこと(ADR-0020 §3)。 */

const CAPS: RagBackendCapabilities = {
  axes: ['citation_granularity', 'filter_expressiveness', 'row_level_security'],
  backends: {
    vector_store: {
      label: 'マネージド Vector Store',
      role: '手軽さ側',
      axes: {
        citation_granularity: {
          support: 'limited', verified: true,
          detail: 'ファイル単位。', evidence: 'SPIKE-M1 ①-a',
        },
        filter_expressiveness: {
          support: 'limited', verified: true,
          detail: '属性フィルタ eq/and/or/gte。', evidence: 'RAGM-01',
        },
        row_level_security: {
          support: 'no', verified: false,
          detail: '行単位の制御機構は無い。', evidence: '比較ドキュメント',
        },
      },
    },
    adb: {
      label: 'Oracle AI Database 自前索引',
      role: '高機能側',
      axes: {
        citation_granularity: {
          support: 'yes', verified: true,
          detail: 'チャンク単位。セル範囲まで返る。', evidence: 'PREP-01',
        },
        filter_expressiveness: {
          support: 'yes', verified: true,
          detail: 'SQL の WHERE。既定で現行版のみ。', evidence: 'RAGM-02',
        },
        row_level_security: {
          support: 'unverified', verified: false,
          detail: 'VPD は未実証。', evidence: 'SPIKE-M1 SKIPPED 3',
        },
      },
      notes: ['ADB 23ai 以上が前提。'],
    },
  },
}

const renderPanel = (backend: string, caps: RagBackendCapabilities | null = CAPS) =>
  render(
    <PrefsProvider>
      <BackendCapabilityPanel backend={backend} caps={caps} />
    </PrefsProvider>,
  )

describe('pickRagBackendCapabilities', () => {
  it('extracts backend_capabilities from the rag.search descriptor', () => {
    const payload = {
      capabilities: [
        { capability: 'chat' },
        { capability: 'rag.search', backend_capabilities: CAPS },
      ],
    }
    expect(pickRagBackendCapabilities(payload)).toEqual(CAPS)
  })

  it('returns null when the catalog has no rag.search capabilities', () => {
    expect(pickRagBackendCapabilities({ capabilities: [{ capability: 'chat' }] })).toBeNull()
    expect(pickRagBackendCapabilities({ capabilities: [{ capability: 'rag.search' }] })).toBeNull()
    expect(pickRagBackendCapabilities(null)).toBeNull()
    expect(pickRagBackendCapabilities('nope')).toBeNull()
  })
})

describe('BackendCapabilityPanel', () => {
  it('shows chunk-level citations and the version filter when adb is selected', () => {
    renderPanel('adb')
    expect(screen.getByText('出典の粒度')).toBeInTheDocument()
    expect(screen.getByText(/チャンク単位。セル範囲まで返る。/)).toBeInTheDocument()
    expect(screen.getByText(/SQL の WHERE。既定で現行版のみ。/)).toBeInTheDocument()
    expect(screen.getByText(/ADB 23ai 以上が前提。/)).toBeInTheDocument()
  })

  it('shows file-level citations for vector_store (switching backend switches the panel)', () => {
    const { rerender } = renderPanel('adb')
    expect(screen.getByTestId('rag-capabilities-adb')).toBeInTheDocument()

    rerender(
      <PrefsProvider>
        <BackendCapabilityPanel backend="vector_store" caps={CAPS} />
      </PrefsProvider>,
    )
    expect(screen.queryByTestId('rag-capabilities-adb')).not.toBeInTheDocument()
    expect(screen.getByText(/ファイル単位。/)).toBeInTheDocument()
    expect(screen.queryByText(/チャンク単位/)).not.toBeInTheDocument()
  })

  it('marks unverified capabilities as unverified, never as available', () => {
    renderPanel('adb')
    // 行レベル制御の行のバッジ(凡例の「未実証」ではなく、能力そのものの表示)
    const badge = screen.getByTitle('SPIKE-M1 SKIPPED 3')
    expect(badge.textContent).toMatch(/未実証/)
    expect(badge.textContent).not.toMatch(/使える/)
  })

  it('renders nothing when the catalog is unavailable or the backend is unknown', () => {
    const { container: noCaps } = renderPanel('adb', null)
    expect(noCaps).toBeEmptyDOMElement()
    const { container: unknown } = renderPanel('does_not_exist')
    expect(unknown).toBeEmptyDOMElement()
  })
})
