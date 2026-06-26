import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { AuthProvider } from '../auth'
import { PrefsProvider } from '../prefs'
import Preview, { CompositionPreview, type DemoComposition } from './preview'

/** HBD-03 デモ構成プレビューの描画証跡。合成結果(API 由来の宣言定義)を実行せずに
 *  描画でき、画面・組込点・使うAI(束縛状態)・使うデータ(シード)が現れることを確認する。
 *  実機 E2E(実 API/DB での合成)は runs/<rid>/e2e/ が担う。 */

const SBA_A: DemoComposition = {
  ok: true,
  sample_app: 'SBA-A',
  instance_id: 'builtin-sba-a',
  app_name: '問い合わせ/サポート管理',
  summary: 'サポートデスク業務アプリ',
  icon: '💬',
  ui: 'chat',
  connectors: ['slack'],
  highlight: 'rag.search',
  screens: [
    {
      key: 'console',
      title: '対応コンソール',
      type: 'detail',
      dataset: 'inquiries',
      slots: [
        { slot_key: 'faq-answer', capability: 'rag.search', title: 'FAQ-RAG 回答', highlight: true },
        { slot_key: 'summarize-thread', capability: 'summarize', title: '問い合わせ要約', highlight: false },
      ],
    },
  ],
  bindings: [
    {
      capability: 'rag.search',
      status: 'active',
      slot_keys: ['faq-answer'],
      screen_keys: ['faq', 'console'],
      title: 'FAQ-RAG 回答',
      highlight: true,
      permissions: ['platform:rag.search'],
      reason: null,
    },
    {
      capability: 'classify',
      status: 'active',
      slot_keys: ['auto-classify'],
      screen_keys: ['inbox'],
      title: '自動分類',
      highlight: false,
      permissions: [],
      reason: null,
    },
  ],
  active_parts: ['rag.search', 'classify'],
  excluded: [],
  seed: {
    strategy: 'sample',
    note: 'コア同梱のサンプルシードをそのまま投入する。',
    seeded: true,
    datasets: [
      { name: 'faqs', label: 'FAQ ナレッジ', fields: 5, seed_rows: 6 },
      { name: 'inquiries', label: '問い合わせ', fields: 9, seed_rows: 4 },
    ],
    total_seed_rows: 10,
  },
  composition_report: {
    ok: true,
    required_capabilities: ['classify', 'rag.search'],
    required_permissions: ['platform:rag.search'],
    missing_capabilities: [],
    undeclared_permissions: [],
    unused_permissions: [],
  },
  warnings: [],
  errors: [],
}

describe('CompositionPreview (HBD-03)', () => {
  it('構成通りに画面・組込点・使うAI・データを描画する', () => {
    render(<CompositionPreview comp={SBA_A} />)

    // ヘッダ: アプリ名・UI・コネクタ
    expect(screen.getByTestId('app-name')).toHaveTextContent('問い合わせ/サポート管理')
    expect(screen.getByText('チャットUI')).toBeInTheDocument()
    expect(screen.getByText(/slack/)).toBeInTheDocument()

    // 画面と組込点: console 画面に RAG 組込点が現れる
    const console = screen.getByTestId('screen-console')
    expect(within(console).getByText('対応コンソール')).toBeInTheDocument()
    expect(within(console).getByText(/FAQ-RAG 回答/)).toBeInTheDocument()
    expect(within(console).getByText(/問い合わせ要約/)).toBeInTheDocument()

    // 使う AI: capability が束縛状態とともに出る
    expect(screen.getByText('rag.search')).toBeInTheDocument()
    expect(screen.getByText('classify')).toBeInTheDocument()
    expect(screen.getAllByText('実行可能').length).toBeGreaterThanOrEqual(2)

    // 使うデータ: シード方針＋データセット行数
    expect(screen.getByText('サンプルシード')).toBeInTheDocument()
    expect(screen.getByText('FAQ ナレッジ')).toBeInTheDocument()
    expect(screen.getByText(/投入予定シード総数: 10 行/)).toBeInTheDocument()
  })

  it('未束縛/組込点なしの部品は警告として描画する', () => {
    const withWarn: DemoComposition = {
      ...SBA_A,
      bindings: [
        ...SBA_A.bindings,
        {
          capability: 'vlm.ocr',
          status: 'no_slot',
          slot_keys: [],
          screen_keys: [],
          title: null,
          highlight: false,
          permissions: [],
          reason: "推薦部品 'vlm.ocr' は主SBA に組込点(aiSlot)が無い",
        },
      ],
      excluded: [{ capability: 'vlm.ocr', status: 'no_slot', reason: '組込点なし' }],
      warnings: ["推薦部品 'vlm.ocr' は主SBA に組込点(aiSlot)が無い"],
    }
    render(<CompositionPreview comp={withWarn} />)
    const warnings = screen.getByTestId('warnings')
    expect(within(warnings).getByText(/vlm.ocr/)).toBeInTheDocument()
    expect(screen.getByText('組込点なし')).toBeInTheDocument()
  })

  it('合成不能(ok=false)はエラーを安全に描画する', () => {
    const failed: DemoComposition = {
      ...SBA_A,
      ok: false,
      instance_id: null,
      app_name: null,
      screens: [],
      bindings: [],
      active_parts: [],
      composition_report: null,
      errors: ['主SBA が未確定(Q1=その他)。最近傍 SBA を確定してから合成してください'],
    }
    render(<CompositionPreview comp={failed} />)
    expect(screen.getByText('合成できませんでした')).toBeInTheDocument()
    expect(within(screen.getByTestId('errors')).getByText(/主SBA が未確定/)).toBeInTheDocument()
  })
})

function jsonResp(body: unknown, status = 200) {
  return Promise.resolve({
    ok: status < 400,
    status,
    json: () => Promise.resolve(body),
  } as Response)
}

function renderPage(sid: string) {
  return render(
    <AuthProvider>
      <PrefsProvider>
        <MemoryRouter initialEntries={[`/preview/${sid}`]}>
          <Routes>
            <Route path="/preview/:sid" element={<Preview />} />
          </Routes>
        </MemoryRouter>
      </PrefsProvider>
    </AuthProvider>,
  )
}

describe('Preview page (薄い統合層)', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('正しい POST URL を叩き、合成結果を描画する', async () => {
    const fetchMock = vi.fn((url: string, opts?: RequestInit) => {
      expect(url).toBe('/api/hearing/sessions/s-123/preview')
      expect(opts?.method).toBe('POST')
      return jsonResp(SBA_A)
    })
    vi.stubGlobal('fetch', fetchMock)
    renderPage('s-123')
    await waitFor(() =>
      expect(screen.getByTestId('app-name')).toHaveTextContent('問い合わせ/サポート管理'),
    )
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('409(推薦なし)を読み込みエラーとして表示する', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => jsonResp({ detail: '推薦がまだありません。先に /recommend を実行してください' }, 409)),
    )
    renderPage('s-no-rec')
    await waitFor(() => expect(screen.getByText('読み込みエラー')).toBeInTheDocument())
    expect(screen.getByText(/推薦がまだありません/)).toBeInTheDocument()
  })
})
