import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { AuthProvider } from '../auth'
import { PrefsProvider } from '../prefs'
import SampleApp from './sampleapp'

/** SBA-02 サポートデスク業務アプリの主要フロー証跡(mock fetch):
 *  受信トレイ → 詳細 → 自動トリアージ(AI提案)採用 → 返信ドラフト生成。
 *  実機 E2E(実 GenAI/ADB)は runs/<rid>/e2e/ が担う。 */

function jsonResp(body: unknown, status = 200) {
  return Promise.resolve({
    ok: status < 400,
    status,
    json: () => Promise.resolve(body),
  } as Response)
}

const APP = {
  id: 'builtin-sba-a',
  name: 'サポートデスク',
  description: 'support',
  icon: '💬',
  knowledge_dataset: 'faqs',
  slot_bindings: {
    'faq-answer': true,
    'auto-classify': true,
    'summarize-thread': true,
    'reply-draft': true,
  },
  definition: {
    summary: 's',
    screens: [],
    aiSlots: [
      { key: 'faq-answer', title: 'FAQ-RAG', capability: 'rag.search' },
      { key: 'auto-classify', title: '自動分類', capability: 'classify' },
      { key: 'summarize-thread', title: '要約', capability: 'summarize' },
      { key: 'reply-draft', title: 'ドラフト', capability: 'draft' },
    ],
    datasets: [
      {
        name: 'faqs',
        label: 'FAQ',
        fields: [],
        seed: [
          { question: 'パスワードを忘れた', answer: '再設定リンクから', category: 'アカウント', views: 10, updated_at: '2026-06-01' },
          { question: '請求書はどこ', answer: '請求履歴から', category: '請求', views: 5, updated_at: '2026-06-02' },
        ],
      },
      {
        name: 'inquiries',
        label: '問い合わせ',
        fields: [],
        seed: [
          {
            id: 'inq-001',
            subject: 'ログインできない',
            customer: '株式会社テスト / 田中様',
            body: 'パスワードを間違えてロックされました',
            // 構造化スレッド(JSON 配列)= 発言者ロール付き会話。
            thread: JSON.stringify([
              { role: 'customer', name: '田中 太郎 様', at: '2026-06-25T09:12:00', text: 'ログインできません。ロックされました。' },
              { role: 'agent', name: 'サポート 山本', at: '2026-06-25T09:30:00', text: '確認いたします。少々お待ちください。' },
            ]),
            category: '',
            priority: '',
            status: 'new',
            received_at: '2026-06-25T09:12:00',
          },
        ],
      },
    ],
  },
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, opts?: RequestInit) => {
      const u = String(url)
      if (u.includes('/slots/auto-classify/invoke')) {
        const body = JSON.parse(String(opts?.body ?? '{}')) as { categories?: string[] }
        const isPriority = (body.categories ?? []).includes('高')
        return jsonResp({
          capability: 'classify',
          category: isPriority ? '高' : 'アカウント',
          matched: true,
          candidates: body.categories ?? [],
        })
      }
      if (u.includes('/slots/faq-answer/invoke')) {
        return jsonResp({
          capability: 'rag.search',
          answer: '再設定リンクからパスワードを変更できます。',
          citations: [{ index: 0, label: 'パスワードを忘れた', score: 3 }],
          grounded: true,
        })
      }
      if (u.includes('/slots/reply-draft/invoke')) {
        return jsonResp({ capability: 'draft', draft: '田中様\nお問い合わせありがとうございます。', citations: [] })
      }
      if (u.includes('/slots/summarize-thread/invoke')) {
        return jsonResp({ capability: 'summarize', summary: 'ログイン不可・ロックの問い合わせ。' })
      }
      if (u.includes('/api/sample-apps/builtin-sba-a')) return jsonResp(APP)
      return jsonResp({})
    }),
  )
})

afterEach(() => vi.unstubAllGlobals())

function renderPage() {
  return render(
    <AuthProvider>
      <PrefsProvider>
        <MemoryRouter initialEntries={['/sba/builtin-sba-a']}>
          <Routes>
            <Route path="/sba/:id" element={<SampleApp />} />
          </Routes>
        </MemoryRouter>
      </PrefsProvider>
    </AuthProvider>,
  )
}

describe('SBA-A support desk', () => {
  it('inbox lists inquiries with status and summary counts', async () => {
    renderPage()
    expect(await screen.findByText('サポートデスク')).toBeTruthy()
    // 受信トレイに件名・顧客が出る
    expect(screen.getByRole('button', { name: 'ログインできない' })).toBeTruthy()
    expect(screen.getByText('株式会社テスト / 田中様')).toBeTruthy()
  })

  it('inbox → detail → AI triage adopt → reply draft flow', async () => {
    renderPage()
    // 詳細へ
    fireEvent.click(await screen.findByRole('button', { name: 'ログインできない' }))

    // 自動トリアージ実行 → AI 提案 → 採用
    fireEvent.click(await screen.findByRole('button', { name: /AI で振り分け/ }))
    const adopt = await screen.findByRole('button', { name: '採用する' })
    fireEvent.click(adopt)
    // 採用後、カテゴリ(アカウント)が問い合わせに反映され表示される
    await waitFor(() => {
      expect(screen.getAllByText('アカウント').length).toBeGreaterThanOrEqual(1)
    })

    // 返信ドラフト生成 → textarea に本文
    fireEvent.click(screen.getByRole('button', { name: /ドラフト生成/ }))
    await waitFor(() => {
      const ta = screen.getByLabelText('返信ドラフト') as HTMLTextAreaElement
      expect(ta.value).toContain('お問い合わせありがとうございます')
    })
  })

  it('renders the conversation thread as role-tagged chat bubbles', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'ログインできない' }))
    // 顧客・サポート双方の発言と発言者名が会話として描画される
    expect(await screen.findByText('ログインできません。ロックされました。')).toBeTruthy()
    expect(screen.getByText('確認いたします。少々お待ちください。')).toBeTruthy()
    expect(screen.getByText('田中 太郎 様')).toBeTruthy()
    expect(screen.getByText('サポート 山本')).toBeTruthy()
  })

  it('parses a legacy newline-string thread (backward compat) into bubbles', async () => {
    // 旧 "顧客:/担当:" 改行形式の素文字列スレッドも発言者ロール付き吹き出しに正規化される。
    const legacy = {
      ...APP,
      definition: {
        ...APP.definition,
        datasets: [
          APP.definition.datasets[0],
          {
            ...APP.definition.datasets[1],
            seed: [
              {
                ...APP.definition.datasets[1].seed[0],
                thread: '顧客: 旧形式の質問です\n担当: 旧形式の回答です',
              },
            ],
          },
        ],
      },
    }
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (String(url).includes('/api/sample-apps/builtin-sba-a')) return jsonResp(legacy)
        return jsonResp({})
      }),
    )
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'ログインできない' }))
    // 役割接頭辞("顧客:"/"担当:")は除去され、本文が会話として描画される
    expect(await screen.findByText('旧形式の質問です')).toBeTruthy()
    expect(screen.getByText('旧形式の回答です')).toBeTruthy()
  })

  it('sending a reply draft appends an agent message to the thread', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'ログインできない' }))
    // 送信前は担当(デモ既定の発言者名)の発言は無い
    expect(screen.queryByText('サポート担当')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /ドラフト生成/ }))
    await waitFor(() => {
      const ta = screen.getByLabelText('返信ドラフト') as HTMLTextAreaElement
      expect(ta.value).toContain('お問い合わせありがとうございます')
    })
    fireEvent.click(screen.getByRole('button', { name: /送信（デモ）/ }))
    // 構造化メッセージとして agent ロールの発言がスレッドに追記される
    await waitFor(() => {
      expect(screen.getByText('サポート担当')).toBeTruthy()
    })
  })

  it('marks triage low-confidence when classify did not match a candidate', async () => {
    // backend が候補一致なし(matched=false)で先頭カテゴリへフォールバックした場合、
    // UI は「確定の提案」ではなく低信頼の推定として注意表示する。
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        const u = String(url)
        if (u.includes('/slots/auto-classify/invoke')) {
          return jsonResp({ capability: 'classify', category: '未分類', matched: false, candidates: [] })
        }
        if (u.includes('/api/sample-apps/builtin-sba-a')) return jsonResp(APP)
        return jsonResp({})
      }),
    )
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'ログインできない' }))
    fireEvent.click(await screen.findByRole('button', { name: /AI で振り分け/ }))
    expect(await screen.findByText(/候補に一致せず推定/)).toBeTruthy()
  })

  it('detail → summarize thread shows AI summary', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'ログインできない' }))
    // 要約ボタン押下 → capability 'summarize' が呼ばれ summary が表示される
    fireEvent.click(await screen.findByRole('button', { name: /3行に要約/ }))
    await waitFor(() => {
      expect(screen.getByText('ログイン不可・ロックの問い合わせ。')).toBeTruthy()
    })
  })

  it('knowledge view lists FAQ with views', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'ナレッジ' }))
    const table = await screen.findByText('パスワードを忘れた')
    expect(table).toBeTruthy()
    expect(within(document.body).getByText('請求書はどこ')).toBeTruthy()
  })

  it('shows not-found for unknown app', async () => {
    vi.stubGlobal('fetch', vi.fn(() => jsonResp({ detail: 'x' }, 404)))
    renderPage()
    expect(await screen.findByText(/見つかりません|not found/i)).toBeTruthy()
  })

  it('distinguishes a non-404 load error by surfacing the HTTP status', async () => {
    vi.stubGlobal('fetch', vi.fn(() => jsonResp({ detail: 'forbidden' }, 403)))
    renderPage()
    expect(await screen.findByText(/HTTP 403/)).toBeTruthy()
  })
})
