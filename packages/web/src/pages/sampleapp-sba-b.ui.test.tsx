import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { AuthProvider } from '../auth'
import { PrefsProvider } from '../prefs'
import SampleApp from './sampleapp'

/** SBA-03 在庫・受発注照会(NL2SQL)業務アプリの主要フロー証跡(mock fetch):
 *  質問 → 生成SQL(nl2sql スロット) → 読取専用実行 → 結果テーブル → グラフ化(chart スロット)。
 *  実機 E2E(実 GenAI/ADB)は runs/<rid>/e2e/ が担う。 */

function jsonResp(body: unknown, status = 200) {
  return Promise.resolve({
    ok: status < 400,
    status,
    json: () => Promise.resolve(body),
  } as Response)
}

const APP = {
  id: 'builtin-sba-b',
  name: '在庫・受発注照会',
  description: 'inventory & orders',
  icon: '📦',
  knowledge_dataset: null,
  slot_bindings: { 'nl2sql-query': true, 'result-chart': true },
  definition: {
    summary: 's',
    screens: [],
    aiSlots: [
      { key: 'nl2sql-query', title: '自然言語DB照会', capability: 'nl2sql' },
      { key: 'result-chart', title: '結果グラフ化', capability: 'chart' },
    ],
    datasets: [
      {
        name: 'inventory',
        label: '在庫',
        fields: [
          { name: 'product_code', type: 'string', label: '商品コード' },
          { name: 'warehouse', type: 'string', label: '倉庫' },
          { name: 'quantity', type: 'number', label: '在庫数' },
        ],
        seed: [
          { product_code: 'P-1001', warehouse: '東京DC', quantity: 320 },
          { product_code: 'P-1002', warehouse: '大阪DC', quantity: 60 },
        ],
      },
      {
        name: 'orders',
        label: '受発注',
        fields: [
          { name: 'order_id', type: 'string', label: '伝票番号' },
          { name: 'partner', type: 'string', label: '取引先' },
          { name: 'amount', type: 'number', label: '金額' },
        ],
        seed: [{ order_id: 'SO-2601', partner: '山田商事', amount: 57600 }],
      },
    ],
  },
}

const chartBodies: Array<Record<string, unknown>> = []

beforeEach(() => {
  chartBodies.length = 0
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, opts?: RequestInit) => {
      const u = String(url)
      if (u.includes('/slots/nl2sql-query/invoke')) {
        return jsonResp({
          capability: 'nl2sql',
          sql: 'SELECT warehouse, SUM(quantity) AS qty FROM INVENTORY GROUP BY warehouse',
        })
      }
      if (u.includes('/slots/result-chart/invoke')) {
        chartBodies.push(JSON.parse(String(opts?.body ?? '{}')) as Record<string, unknown>)
        // type=none を返し jsdom の canvas 描画を避けつつ、chart スロット連携を検証する。
        return jsonResp({ capability: 'chart', type: 'none', x: null, y: [], title: '', reason: '数値列が1つのみ' })
      }
      if (u.includes('/dbchat/execute')) {
        return jsonResp({
          columns: ['WAREHOUSE', 'QTY'],
          rows: [
            ['東京DC', '320'],
            ['大阪DC', '60'],
          ],
          row_count: 2,
          truncated: false,
        })
      }
      if (u.includes('/api/sample-apps/builtin-sba-b')) return jsonResp(APP)
      return jsonResp({})
    }),
  )
})

afterEach(() => vi.unstubAllGlobals())

function renderPage() {
  return render(
    <AuthProvider>
      <PrefsProvider>
        <MemoryRouter initialEntries={['/sba/builtin-sba-b']}>
          <Routes>
            <Route path="/sba/:id" element={<SampleApp />} />
          </Routes>
        </MemoryRouter>
      </PrefsProvider>
    </AuthProvider>,
  )
}

describe('SBA-B inventory/orders NL2SQL console', () => {
  it('dispatches to the NL2SQL console (not support desk) for nl2sql apps', async () => {
    renderPage()
    expect(await screen.findByText('在庫・受発注照会')).toBeTruthy()
    // 照会コンソールの質問入力(SBA-B 固有)が出る。
    expect(screen.getByPlaceholderText('例: 倉庫別の在庫数を集計して')).toBeTruthy()
  })

  it('inventory tab lists seed rows', async () => {
    renderPage()
    await screen.findByText('在庫・受発注照会')
    fireEvent.click(screen.getByRole('button', { name: '在庫' }))
    expect(await screen.findByText('P-1001')).toBeTruthy()
    expect(screen.getByText('P-1002')).toBeTruthy()
  })

  it('question → generate SQL → execute → result table → chart slot', async () => {
    renderPage()
    await screen.findByText('在庫・受発注照会')

    // 質問入力 → SQL生成(nl2sql スロット)
    const ta = screen.getByPlaceholderText('例: 倉庫別の在庫数を集計して')
    fireEvent.change(ta, { target: { value: '倉庫別の在庫数を集計して' } })
    fireEvent.click(screen.getByRole('button', { name: 'SQL生成' }))

    const sqlBox = (await screen.findByLabelText('生成されたSQL（編集できます）')) as HTMLTextAreaElement
    await waitFor(() => expect(sqlBox.value).toContain('SELECT'))

    // 読取専用実行 → 結果テーブル
    fireEvent.click(screen.getByRole('button', { name: /実行/ }))
    await waitFor(() => expect(screen.getByText('WAREHOUSE')).toBeTruthy())
    expect(screen.getByText('QTY')).toBeTruthy()

    // グラフ化(chart スロット)→ columns/rows がスロットへ渡る
    fireEvent.click(screen.getByRole('button', { name: /グラフ化/ }))
    await waitFor(() => expect(chartBodies.length).toBe(1))
    expect(chartBodies[0].columns).toEqual(['WAREHOUSE', 'QTY'])
    expect(Array.isArray(chartBodies[0].rows)).toBe(true)
  })

  it('surfaces an execute guard error (e.g. non-select rejected)', async () => {
    renderPage()
    await screen.findByText('在庫・受発注照会')
    const ta = screen.getByPlaceholderText('例: 倉庫別の在庫数を集計して')
    fireEvent.change(ta, { target: { value: 'x' } })
    fireEvent.click(screen.getByRole('button', { name: 'SQL生成' }))
    await screen.findByLabelText('生成されたSQL（編集できます）')

    // 実行時に 400(ガード)を返すと UI にエラーが表示される。
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (String(url).includes('/dbchat/execute')) {
          return jsonResp({ detail: 'SELECT文のみ実行できます' }, 400)
        }
        return jsonResp({})
      }),
    )
    fireEvent.click(screen.getByRole('button', { name: /実行/ }))
    expect(await screen.findByText(/SELECT文のみ実行できます/)).toBeTruthy()
  })

  it('execute handles 401 like slot invoke (M1): surfaces session-lost', async () => {
    renderPage()
    await screen.findByText('在庫・受発注照会')
    const ta = screen.getByPlaceholderText('例: 倉庫別の在庫数を集計して')
    fireEvent.change(ta, { target: { value: 'x' } })
    fireEvent.click(screen.getByRole('button', { name: 'SQL生成' }))
    await screen.findByLabelText('生成されたSQL（編集できます）')

    // 専用 execute が 401 を返したら、slot invoke と同じく再認証＋セッション切れ表示にする。
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (String(url).includes('/dbchat/execute')) {
          return jsonResp({ detail: 'unauthorized' }, 401)
        }
        return jsonResp({})
      }),
    )
    fireEvent.click(screen.getByRole('button', { name: /実行/ }))
    expect(await screen.findByText(/セッションの有効期限が切れました/)).toBeTruthy()
  })
})
