import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { AuthProvider } from '../auth'
import { PrefsProvider } from '../prefs'
import SampleApp from './sampleapp'

/** SBA-04 営業案件管理(SBA-C)の複合AI連動フロー証跡(mock fetch):
 *  パイプライン → 案件コンソール → 議事録要約 → 次アクション提案 → メール下書き、
 *  および売上分析(NL2SQL)。実機 E2E(実 GenAI/ADB)は runs/<rid>/e2e/ が担う。 */

function jsonResp(body: unknown, status = 200) {
  return Promise.resolve({
    ok: status < 400,
    status,
    json: () => Promise.resolve(body),
  } as Response)
}

const APP_C = {
  id: 'builtin-sba-c',
  name: '営業案件管理',
  description: 'sales',
  icon: '📊',
  slot_bindings: {
    'minutes-summary': true,
    'next-actions': true,
    'sales-rollup': true,
    'email-draft': true,
  },
  definition: {
    summary: 's',
    screens: [],
    aiSlots: [
      { key: 'minutes-summary', title: '議事録要約', capability: 'minutes' },
      { key: 'next-actions', title: '次アクション提案', capability: 'agent' },
      { key: 'sales-rollup', title: '売上集計', capability: 'nl2sql' },
      { key: 'email-draft', title: 'メール下書き', capability: 'draft' },
    ],
    datasets: [
      {
        name: 'deals',
        label: '案件',
        fields: [],
        seed: [
          {
            id: 'deal-001',
            name: '山田製作所 — MES連携',
            customer: '山田製作所',
            stage: '提案',
            amount: 12000000,
            probability: 60,
            owner: '佐々木',
            close_date: '2026-08-29',
            next_step: 'PoC範囲の合意',
          },
        ],
      },
      {
        name: 'meetings',
        label: '議事録',
        fields: [],
        seed: [
          {
            id: 'mtg-001',
            deal_id: 'deal-001',
            title: '第2回提案レビュー',
            date: '2026-06-24',
            attendees: '山田部長',
            notes: 'MESが老朽化。クラウド移行に前向き。',
          },
        ],
      },
      { name: 'sales', label: '売上', fields: [], seed: [] },
    ],
  },
}

function slotResp(url: string) {
  if (url.includes('/slots/minutes-summary/'))
    return jsonResp({ capability: 'minutes', summary: '## 要点\nMES老朽化、移行に前向き' })
  if (url.includes('/slots/next-actions/'))
    return jsonResp({ capability: 'agent', actions: ['[今週] PoC対象を2本に絞る'] })
  if (url.includes('/slots/email-draft/'))
    return jsonResp({ capability: 'draft', draft: '山田製作所 ご担当者様\n\n本日はありがとうございました。' })
  if (url.includes('/slots/sales-rollup/'))
    return jsonResp({
      capability: 'nl2sql',
      schema: 'JETUSE_SBA04',
      sql: 'SELECT owner, SUM(amount) FROM JETUSE_SBA04.SALES GROUP BY owner',
      columns: ['OWNER', 'TOTAL'],
      rows: [['加藤', '62900000']],
      row_count: 1,
      truncated: false,
    })
  return jsonResp({ detail: 'unknown slot' }, 404)
}

function renderApp() {
  return render(
    <AuthProvider>
      <PrefsProvider>
        <MemoryRouter initialEntries={['/sba/builtin-sba-c']}>
          <Routes>
            <Route path="/sba/:id" element={<SampleApp />} />
          </Routes>
        </MemoryRouter>
      </PrefsProvider>
    </AuthProvider>,
  )
}

describe('SBA-C 営業案件管理(複合AI)', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        const u = String(url)
        if (u.includes('/slots/')) return slotResp(u)
        if (u.includes('/api/sample-apps/builtin-sba-c')) return jsonResp(APP_C)
        return jsonResp({}, 404)
      }),
    )
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('議事録要約 → 次アクション → メール下書きが連動する', async () => {
    renderApp()
    // パイプラインに案件が出る
    const dealLink = await screen.findByText('山田製作所 — MES連携')
    fireEvent.click(dealLink)

    // 案件コンソール: 議事録が表示される
    expect(await screen.findByText(/MESが老朽化/)).toBeTruthy()

    // 1) 議事録要約
    fireEvent.click(screen.getByText('AI で要約'))
    expect(await screen.findByText(/MES老朽化、移行に前向き/)).toBeTruthy()

    // 2) 次アクション提案
    fireEvent.click(screen.getByText('AI で提案'))
    expect(await screen.findByText(/PoC対象を2本に絞る/)).toBeTruthy()

    // 3) メール下書き
    fireEvent.click(screen.getByText('🤖 メールを下書き'))
    await waitFor(() =>
      expect((screen.getByLabelText('メール下書き') as HTMLTextAreaElement).value).toContain(
        '山田製作所 ご担当者様',
      ),
    )
  })

  it('案件コンソールで売上集計→メール下書きが連動する(下書き入力に NL2SQL 結果が含まれる)', async () => {
    const bodies: Record<string, string> = {}
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, init?: RequestInit) => {
        const u = String(url)
        if (u.includes('/slots/')) {
          if (init?.body) bodies[u] = String(init.body)
          return slotResp(u)
        }
        if (u.includes('/api/sample-apps/builtin-sba-c')) return jsonResp(APP_C)
        return jsonResp({}, 404)
      }),
    )
    renderApp()
    fireEvent.click(await screen.findByText('山田製作所 — MES連携'))
    // 売上集計(NL2SQL)を案件コンソールで実行
    fireEvent.click(await screen.findByText('担当者別売上'))
    expect(await screen.findByText('62900000')).toBeTruthy()
    // メール下書き → 入力(body)に売上集計結果が織り込まれている(4 能力が UI でも連動)
    fireEvent.click(screen.getByText('🤖 メールを下書き'))
    await waitFor(() => {
      const draftBody = Object.entries(bodies).find(([u]) => u.includes('/slots/email-draft/'))?.[1]
      expect(draftBody).toBeTruthy()
      expect(draftBody).toContain('売上参考')
      expect(draftBody).toContain('加藤')
      expect(draftBody).toContain('62900000')
    })
  })

  it('売上分析で NL2SQL(専用スキーマ)を照会し結果表を表示する', async () => {
    renderApp()
    fireEvent.click(await screen.findByText('売上分析'))
    fireEvent.click(await screen.findByText('🤖 集計する'))
    // SQL と結果表
    expect(await screen.findByText(/JETUSE_SBA04.SALES/)).toBeTruthy()
    expect(await screen.findByText('62900000')).toBeTruthy()
  })
})
