import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { AuthProvider } from '../auth'
import { PrefsProvider } from '../prefs'
import Preview, {
  LaunchPanel,
  SummaryPanel,
  ValidationPanel,
  type DemoComposition,
  type DemoLaunch,
  type DemoSummary,
  type GovernanceReport,
} from './preview'

/** HBD-05 一気通貫(プレビュー→検証→起動→サマリ)の導線証跡。各純粋コンポーネントの描画と、
 *  ページの順次操作(検証 PASS で起動可、FAIL で起動不可＋代替提案)を確認する。
 *  実機 E2E(実 API/DB/GenAI)は runs/<rid>/e2e/ が担う。 */

const COMP: DemoComposition = {
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
      slots: [{ slot_key: 'faq', capability: 'rag.search', title: 'FAQ-RAG 回答', highlight: true }],
    },
  ],
  bindings: [
    {
      capability: 'rag.search',
      status: 'active',
      slot_keys: ['faq'],
      screen_keys: ['console'],
      title: 'FAQ-RAG 回答',
      highlight: true,
      permissions: ['platform:rag.search'],
      reason: null,
    },
  ],
  active_parts: ['rag.search'],
  excluded: [],
  seed: {
    strategy: 'sample',
    note: 'コア同梱のサンプルシードをそのまま投入する。',
    seeded: true,
    datasets: [{ name: 'faqs', label: 'FAQ ナレッジ', fields: 5, seed_rows: 6 }],
    total_seed_rows: 6,
  },
  composition_report: null,
  warnings: [],
  errors: [],
}

const GOV_OK: GovernanceReport = {
  ok: true,
  sample_app: 'SBA-A',
  violations: [],
  checks: {
    allowed_combination: true,
    capabilities_bound: true,
    permission_scope: true,
    model_available: true,
  },
}

const GOV_FAIL: GovernanceReport = {
  ok: false,
  sample_app: 'SBA-A',
  violations: [
    {
      kind: 'disallowed_combination',
      element: 'nl2sql',
      element_type: 'capability',
      detail: "主SBA 'SBA-A' に 'nl2sql' の組込点が無い(許可外組合せ)",
      alternative: "'nl2sql' を活かすには主アプリを SBA-B にする",
    },
  ],
  checks: {
    allowed_combination: false,
    capabilities_bound: true,
    permission_scope: true,
    model_available: true,
  },
}

const LAUNCH: DemoLaunch = {
  id: 'l-1',
  session_id: 's-1',
  sample_app: 'SBA-A',
  instance_id: 'builtin-sba-a',
  entry_slot: 'faq',
  demo_url: '/sba/builtin-sba-a',
  status: 'launched',
}

const SUMMARY: DemoSummary = {
  sample_app: 'SBA-A',
  app_name: '問い合わせ/サポート管理',
  ui: 'chat',
  connectors: ['slack'],
  highlight: 'rag.search',
  seed_strategy: 'sample',
  diagram: [
    {
      data: 'FAQ ナレッジ',
      capability: 'rag.search',
      capability_label: '社内文書 RAG 検索（根拠付き QA）',
      screen: '対応コンソール',
      highlight: true,
      line: 'FAQ ナレッジ → 社内文書 RAG 検索（根拠付き QA）（対応コンソール 画面）',
    },
  ],
  oci_services: [
    { service: 'Oracle Autonomous Database（アプリ定義・業務/シードデータ）', used_for: ['アプリ定義・業務データの永続'] },
    { service: 'OCI Generative AI（埋め込み + File Search / Vector Store）', used_for: ['社内文書 RAG 検索（根拠付き QA）'] },
  ],
  steps: [
    { order: 1, title: 'デモアプリ「問い合わせ/サポート管理」を起動環境で開く', detail: 'UI: chat' },
    { order: 2, title: '「対応コンソール」画面で RAG 検索 を実行する（主役 AI 機能）', detail: 'FAQ に効く' },
  ],
  impact: '想定効果の文章。',
  impact_source: 'genai',
  active_parts: ['rag.search'],
  excluded: [],
  markdown: '# 構成サマリ: 問い合わせ/サポート管理（SBA-A）\n## ① 構成図\n...',
}

describe('ValidationPanel / LaunchPanel / SummaryPanel (HBD-05 純粋描画)', () => {
  it('検証 PASS はチェックを全て緑で出す', () => {
    render(<ValidationPanel gov={GOV_OK} />)
    expect(screen.getByText('✓ 検証 PASS')).toBeInTheDocument()
    expect(within(screen.getByTestId('gov-checks')).getByText(/許可された組合せ/)).toBeInTheDocument()
  })

  it('検証 FAIL は違反と代替提案を出す(外させない)', () => {
    render(<ValidationPanel gov={GOV_FAIL} />)
    expect(screen.getByText('✗ 検証 FAIL')).toBeInTheDocument()
    const v = screen.getByTestId('gov-violations')
    expect(within(v).getByText(/組込点が無い/)).toBeInTheDocument()
    expect(within(v).getByText(/代替提案/)).toBeInTheDocument()
  })

  it('起動パネルは主役 AI 機能の実行導線(demo_url)を出す', () => {
    render(
      <MemoryRouter>
        <LaunchPanel launch={LAUNCH} />
      </MemoryRouter>,
    )
    const link = screen.getByTestId('run-demo-link')
    expect(link).toHaveAttribute('href', '/sba/builtin-sba-a')
  })

  it('サマリは 4 項目(構成図/OCIサービス/手順/効果)を描画する', () => {
    render(<SummaryPanel summary={SUMMARY} onExport={() => {}} />)
    expect(within(screen.getByTestId('summary-diagram')).getByText(/RAG 検索/)).toBeInTheDocument()
    expect(screen.getByText(/File Search/)).toBeInTheDocument()
    expect(within(screen.getByTestId('summary-steps')).getByText(/主役 AI 機能/)).toBeInTheDocument()
    expect(screen.getByTestId('summary-impact')).toHaveTextContent('想定効果の文章。')
  })
})

function jsonResp(body: unknown, status = 200) {
  return Promise.resolve({
    ok: status < 400,
    status,
    json: () => Promise.resolve(body),
  } as Response)
}

function textResp(text: string, status = 200) {
  return Promise.resolve({
    ok: status < 400,
    status,
    text: () => Promise.resolve(text),
    json: () => Promise.resolve({}),
  } as Response)
}

function renderPage(sid: string) {
  return render(
    <AuthProvider>
      <PrefsProvider>
        <MemoryRouter initialEntries={[`/preview/${sid}`]}>
          <Routes>
            <Route path="/preview/:sid" element={<Preview />} />
            <Route path="/sba/:id" element={<div>SBA RUN PAGE</div>} />
          </Routes>
        </MemoryRouter>
      </PrefsProvider>
    </AuthProvider>,
  )
}

/** URL+method でルーティングする fetch モック。 */
function flowFetch(gov: GovernanceReport, opts?: { launchStatus?: number }) {
  return vi.fn((url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    if (url.endsWith('/preview') && method === 'POST') return jsonResp(COMP)
    if (url.endsWith('/validate') && method === 'POST')
      return jsonResp({ composition: COMP, governance: gov })
    if (url.endsWith('/launch') && method === 'POST') {
      if (opts?.launchStatus === 409) {
        return jsonResp(
          { detail: { message: 'バリデーション未通過のため起動できません', governance: gov } },
          409,
        )
      }
      return jsonResp({ launch: LAUNCH, composition: COMP, governance: gov })
    }
    if (url.endsWith('/summary') && method === 'POST') return jsonResp(SUMMARY)
    if (url.endsWith('/summary/export') && method === 'GET')
      return textResp('# 構成サマリ（エクスポート）\n## ① 構成図\n...')
    throw new Error(`unexpected fetch: ${method} ${url}`)
  })
}

describe('Preview 一気通貫(検証→起動→サマリ)', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('検証 PASS で起動でき、起動後に実行導線が出る', async () => {
    vi.stubGlobal('fetch', flowFetch(GOV_OK))
    renderPage('s-1')
    await waitFor(() =>
      expect(screen.getByTestId('app-name')).toHaveTextContent('問い合わせ/サポート管理'),
    )
    // 起動は検証 PASS まで無効。
    const launchBtn = screen.getByRole('button', { name: 'このデモを起動' })
    expect(launchBtn).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: '構成を検証する' }))
    await waitFor(() => expect(screen.getByText('✓ 検証 PASS')).toBeInTheDocument())
    expect(launchBtn).not.toBeDisabled()

    fireEvent.click(launchBtn)
    await waitFor(() => expect(screen.getByTestId('run-demo-link')).toBeInTheDocument())
    expect(screen.getByTestId('run-demo-link')).toHaveAttribute('href', '/sba/builtin-sba-a')
  })

  it('構成サマリを生成して 4 項目を表示する', async () => {
    vi.stubGlobal('fetch', flowFetch(GOV_OK))
    renderPage('s-1')
    await waitFor(() => expect(screen.getByTestId('app-name')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: '構成サマリを生成' }))
    await waitFor(() => expect(screen.getByTestId('summary-diagram')).toBeInTheDocument())
    expect(screen.getByTestId('summary-impact')).toHaveTextContent('想定効果の文章。')
    expect(screen.getByTestId('export-summary')).toBeInTheDocument()
  })

  it('エクスポートはサーバの正準 GET /summary/export を取得する(F-004)', async () => {
    const fetchMock = flowFetch(GOV_OK)
    vi.stubGlobal('fetch', fetchMock)
    // jsdom には createObjectURL が無いのでスタブする。
    vi.stubGlobal('URL', {
      ...URL,
      createObjectURL: vi.fn(() => 'blob:mock'),
      revokeObjectURL: vi.fn(),
    } as unknown as typeof URL)
    renderPage('s-1')
    await waitFor(() => expect(screen.getByTestId('app-name')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: '構成サマリを生成' }))
    await waitFor(() => expect(screen.getByTestId('export-summary')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('export-summary'))
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([u, o]) => String(u).endsWith('/summary/export') && (o?.method ?? 'GET') === 'GET',
        ),
      ).toBe(true),
    )
  })

  it('境界: 検証 FAIL では起動できず、代替提案へ誘導する', async () => {
    vi.stubGlobal('fetch', flowFetch(GOV_FAIL))
    renderPage('s-1')
    await waitFor(() => expect(screen.getByTestId('app-name')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: '構成を検証する' }))
    await waitFor(() => expect(screen.getByText('✗ 検証 FAIL')).toBeInTheDocument())
    // 起動は無効のまま、代替提案が表示される。
    expect(screen.getByRole('button', { name: 'このデモを起動' })).toBeDisabled()
    expect(screen.getByTestId('launch-blocked-note')).toBeInTheDocument()
    expect(within(screen.getByTestId('gov-violations')).getByText(/代替提案/)).toBeInTheDocument()
  })
})
