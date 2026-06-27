import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useSearchParams } from 'react-router-dom'
import { AuthProvider } from '../auth'
import { PrefsProvider } from '../prefs'
import Hearing from './hearing'

/** HBD-02 ダイアログ式ヒアリングUI の主要分岐証跡(mock fetch):
 *  入力(メモ→AI提案) → 順次Q&A(進捗/分岐) → 確定 → 推薦構成の提示。
 *  実機 E2E(実 GenAI/ADB往復・回答保存)は runs/<rid>/e2e/ が担う。 */

function jsonResp(body: unknown, status = 200) {
  return Promise.resolve({
    ok: status < 400,
    status,
    json: () => Promise.resolve(body),
  } as Response)
}

const QUESTIONS = {
  version: '1',
  questions: [
    {
      id: 'Q1', type: 'single', text: 'この顧客で AI を効かせたい業務は？', purpose: '主SBA決定',
      required: true, min_selections: 1,
      options: [
        { id: 'support', label: '顧客対応/サポート' },
        { id: 'inventory', label: '在庫・受発注・データ照会' },
        { id: 'other', label: 'その他(自由)' },
      ],
    },
    {
      id: 'Q2', type: 'multi', text: '扱う主なデータはどこに？', purpose: 'AI部品の素地',
      required: true, min_selections: 1,
      options: [
        { id: 'docs', label: '社内文書/FAQ/マニュアル' },
        { id: 'business_db', label: '業務DB(表・基幹)' },
      ],
    },
    {
      id: 'Q3', type: 'single', text: '顧客が一番見たい AI の効き所は？', purpose: '主役AI',
      required: true, min_selections: 1,
      options: [
        { id: 'rag_qa', label: '質問に答える(RAG-QA)' },
        { id: 'nl2sql', label: '自然言語で集計・分析(NL2SQL)' },
      ],
    },
    {
      id: 'Q4', type: 'single', text: '既存システム/SaaS連携の希望は？', purpose: 'コネクタ',
      required: true, min_selections: 1,
      options: [
        { id: 'slack', label: 'Slack 通知/起動' },
        { id: 'none', label: 'なし(スタンドアロン)' },
      ],
    },
    {
      id: 'Q5', type: 'single', text: 'デモの利用シーン/出力形態は？', purpose: 'UI',
      required: true, min_selections: 1,
      options: [
        { id: 'chat_form', label: '画面で対話(チャット/フォーム)' },
        { id: 'report', label: 'レポート/帳票出力' },
      ],
    },
    {
      id: 'Q6', type: 'single', text: 'デモ用データはどうする？', purpose: 'シード',
      required: true, min_selections: 1,
      options: [
        { id: 'sample', label: 'サンプルシードでOK' },
        { id: 'replace_later', label: '顧客実データ風を後で差替' },
      ],
    },
    { id: 'Auto', type: 'auto', text: '(自動)チェック', purpose: '', required: false, min_selections: 1, options: [] },
  ],
}

const REC_SBA_B = {
  sample_app: 'SBA-B',
  secondary_sample_apps: [],
  ai_parts: ['rag.search', 'nl2sql', 'chart'],
  not_applicable_parts: [],
  highlight: 'nl2sql',
  connectors: ['slack'],
  ui: 'chat',
  seed_strategy: 'sample',
  needs_genai_nearest: false,
  rationale: ['Q1=support → 主 SBA SBA-A', '分岐(§3): Q2 に業務DB＋Q3=集計分析 → 主役を SBA-A→SBA-B に格上げ'],
  validation: { ok: true, missing_capabilities: [], warnings: [] },
  confirmed_at: null,
}

// 保存された回答 PUT を捕捉して回答保存の疎通を検証する。
const savedAnswers: Array<{ qid: string; value: unknown }> = []

function installFetch(extra?: (url: string, opts?: RequestInit) => Promise<Response> | null) {
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, opts?: RequestInit) => {
      const u = String(url)
      const method = (opts?.method ?? 'GET').toUpperCase()
      const over = extra?.(u, opts)
      if (over) return over
      if (u.endsWith('/api/hearing/questions')) return jsonResp(QUESTIONS)
      if (u.endsWith('/api/hearing/sessions') && method === 'POST') {
        return jsonResp({ id: 'sess-1', status: 'draft', input_notes: '', answers: [] })
      }
      if (u.includes('/sessions/sess-1/suggest')) {
        return jsonResp({
          suggestions: { Q1: 'support', Q2: ['docs'], Q3: 'rag_qa', Q4: 'slack', Q5: 'chat_form', Q6: 'sample' },
          saved: ['Q1', 'Q2', 'Q3', 'Q4', 'Q5', 'Q6'],
          skipped_existing: [],
          genai: 'ok',
        })
      }
      const m = /\/sessions\/sess-1\/answers\/(\w+)/.exec(u)
      if (m && method === 'PUT') {
        const value = (JSON.parse(String(opts?.body ?? '{}')) as { value: unknown }).value
        savedAnswers.push({ qid: m[1], value })
        return jsonResp({ question_id: m[1], value, source: 'sa' })
      }
      if (u.includes('/sessions/sess-1/recommend/confirm')) return jsonResp({ confirmed: true })
      if (u.includes('/sessions/sess-1/recommend')) return jsonResp(REC_SBA_B)
      return jsonResp({})
    }),
  )
}

beforeEach(() => {
  savedAnswers.length = 0
})
afterEach(() => vi.unstubAllGlobals())

function renderPage(entry = '/hearing') {
  return render(
    <AuthProvider>
      <PrefsProvider>
        <MemoryRouter initialEntries={[entry]}>
          <Routes>
            <Route path="/hearing" element={<Hearing />} />
          </Routes>
        </MemoryRouter>
      </PrefsProvider>
    </AuthProvider>,
  )
}

// 回答保存(PUT)中はナビゲーションがロックされる。保存が完了して「次へ/確定」が
// 再び押せるようになるまで待ってから次の操作へ進む(保存と画面遷移の競合防止の検証)。
async function settleAfterSelect() {
  await waitFor(() => {
    const btn =
      screen.queryByRole('button', { name: /次へ/ }) ??
      screen.getByRole('button', { name: /確定して推薦/ })
    expect((btn as HTMLButtonElement).disabled).toBe(false)
  })
}

describe('HBD-02 hearing dialog UI', () => {
  it('input step: paste notes → AI suggestions prefill Q1 with a suggested badge', async () => {
    installFetch()
    renderPage()
    const ta = await screen.findByPlaceholderText(/製造業の顧客/)
    fireEvent.change(ta, { target: { value: '社内マニュアルの問い合わせが多い' } })
    fireEvent.click(screen.getByRole('button', { name: /AI提案で開始/ }))

    // Q1 が提案つきで表示され、進捗は 6/6(全質問が提案で埋まる)。
    expect(await screen.findByText('この顧客で AI を効かせたい業務は？')).toBeTruthy()
    expect(screen.getByText(/AI提案/)).toBeTruthy()
    expect(screen.getByText('6/6')).toBeTruthy()
    // 提案された選択肢(support)が選択状態。
    const opt = screen.getByRole('radio', { name: /顧客対応\/サポート/ })
    expect(opt.getAttribute('aria-checked')).toBe('true')
  })

  it('sequential Q&A with branch (Q2 business_db × Q3 nl2sql → SBA-B) → recommend shows config', async () => {
    installFetch()
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /提案なしで開始/ }))

    // Q1=support
    await screen.findByText('この顧客で AI を効かせたい業務は？')
    fireEvent.click(screen.getByRole('radio', { name: /顧客対応\/サポート/ }))
    await settleAfterSelect()
    fireEvent.click(screen.getByRole('button', { name: /次へ/ }))

    // Q2=business_db (multi)
    await screen.findByText('扱う主なデータはどこに？')
    fireEvent.click(screen.getByRole('checkbox', { name: /業務DB/ }))
    await settleAfterSelect()
    fireEvent.click(screen.getByRole('button', { name: /次へ/ }))

    // Q3=nl2sql → 分岐ノート(SBA-B 格上げ)が出る
    await screen.findByText('顧客が一番見たい AI の効き所は？')
    fireEvent.click(screen.getByRole('radio', { name: /NL2SQL/ }))
    expect(await screen.findByText(/SBA-B（NL2SQL）を主役に格上げ/)).toBeTruthy()
    await settleAfterSelect()
    fireEvent.click(screen.getByRole('button', { name: /次へ/ }))

    // Q4=slack, Q5=chat_form, Q6=sample
    await screen.findByText('既存システム/SaaS連携の希望は？')
    fireEvent.click(screen.getByRole('radio', { name: /Slack/ }))
    await settleAfterSelect()
    fireEvent.click(screen.getByRole('button', { name: /次へ/ }))
    await screen.findByText('デモの利用シーン/出力形態は？')
    fireEvent.click(screen.getByRole('radio', { name: /画面で対話/ }))
    await settleAfterSelect()
    fireEvent.click(screen.getByRole('button', { name: /次へ/ }))
    await screen.findByText('デモ用データはどうする？')
    fireEvent.click(screen.getByRole('radio', { name: /サンプルシードでOK/ }))
    await settleAfterSelect()

    // 確定 → 推薦構成の提示(主SBA=SBA-B, AI部品, コネクタ)
    fireEvent.click(screen.getByRole('button', { name: /確定して推薦/ }))
    expect(await screen.findByText('推薦構成')).toBeTruthy()
    expect(screen.getByText('SBA-B')).toBeTruthy()
    expect(screen.getAllByText(/NL2SQL/).length).toBeGreaterThan(0)
    // 全 6 問が API に保存されている(疎通)。次へ/推薦で sa 確定保存するため重複 PUT あり。
    expect(new Set(savedAnswers.map((a) => a.qid))).toEqual(
      new Set(['Q1', 'Q2', 'Q3', 'Q4', 'Q5', 'Q6']),
    )
    expect(savedAnswers.find((a) => a.qid === 'Q2')?.value).toEqual(['business_db'])

    // SA が推薦を確定(ブラックボックス化しない: 提示→確定の明示)
    fireEvent.click(screen.getByRole('button', { name: /この構成で確定/ }))
    expect(await screen.findByText(/確定済み/)).toBeTruthy()

    // 「回答を修正」で Q&A へ戻ると、確定状態と旧推薦は無効化され再推薦が必要になる。
    fireEvent.click(screen.getByRole('button', { name: /回答を修正/ }))
    expect(await screen.findByText('デモ用データはどうする？')).toBeTruthy()
    expect(screen.queryByText(/確定済み/)).toBeNull()
    expect(screen.queryByText('推薦構成')).toBeNull()
  })

  it('promotes accepted AI suggestions to source=sa before recommending', async () => {
    installFetch()
    renderPage()
    const ta = await screen.findByPlaceholderText(/製造業の顧客/)
    fireEvent.change(ta, { target: { value: '社内マニュアルの問い合わせが多い' } })
    fireEvent.click(screen.getByRole('button', { name: /AI提案で開始/ }))
    await screen.findByText('この顧客で AI を効かせたい業務は？')

    // SA が提案を1つも変更せず、最後の質問まで進んで確定する。
    fireEvent.click(screen.getByRole('button', { name: 'Q6' })) // ステップドットで Q6 へ
    await screen.findByText('デモ用データはどうする？')
    fireEvent.click(screen.getByRole('button', { name: /確定して推薦/ }))
    expect(await screen.findByText('推薦構成')).toBeTruthy()

    // 未修正の提案も含め全 6 問が PUT(=source sa 昇格)されている。
    expect(new Set(savedAnswers.map((a) => a.qid))).toEqual(
      new Set(['Q1', 'Q2', 'Q3', 'Q4', 'Q5', 'Q6']),
    )
    // SA が触っていない Q1 も提案値 support が sa として保存される。
    expect(savedAnswers.find((a) => a.qid === 'Q1')?.value).toBe('support')
  })

  it('rolls back a selection when the answer PUT fails (no unsaved progress)', async () => {
    installFetch((u, opts) => {
      if (/\/answers\/Q1/.test(u) && (opts?.method ?? '').toUpperCase() === 'PUT') {
        return jsonResp({ detail: '保存に失敗しました' }, 500)
      }
      return null
    })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /提案なしで開始/ }))
    await screen.findByText('この顧客で AI を効かせたい業務は？')
    const opt = screen.getByRole('radio', { name: /顧客対応\/サポート/ })
    fireEvent.click(opt)
    // 保存失敗 → エラー表示・選択はロールバック・進捗は進まない(次へ不可)。
    expect(await screen.findByText(/保存に失敗しました/)).toBeTruthy()
    await waitFor(() => expect(opt.getAttribute('aria-checked')).toBe('false'))
    expect(screen.getByText('0/6')).toBeTruthy()
    expect((screen.getByRole('button', { name: /次へ/ }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('renders FastAPI 422 array-style detail (not [object Object])', async () => {
    installFetch((u, opts) => {
      if (/\/answers\/Q1/.test(u) && (opts?.method ?? '').toUpperCase() === 'PUT') {
        return jsonResp(
          { detail: [{ loc: ['body', 'value'], msg: 'メモが長すぎます', type: 'value_error' }] },
          422,
        )
      }
      return null
    })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /提案なしで開始/ }))
    await screen.findByText('この顧客で AI を効かせたい業務は？')
    fireEvent.click(screen.getByRole('radio', { name: /顧客対応\/サポート/ }))
    expect(await screen.findByText(/メモが長すぎます/)).toBeTruthy()
    expect(screen.queryByText(/\[object Object\]/)).toBeNull()
  })

  it('resume via ?sid= restores answers and an existing recommendation', async () => {
    installFetch((u, opts) => {
      if (u.includes('/sessions/sess-1') && (opts?.method ?? 'GET').toUpperCase() === 'GET') {
        return jsonResp({
          id: 'sess-1',
          input_notes: 'メモ',
          answers: [
            { question_id: 'Q1', value: 'support', source: 'sa' },
            { question_id: 'Q3', value: 'nl2sql', source: 'genai_suggested' },
          ],
          recommendation: REC_SBA_B,
        })
      }
      return null
    })
    renderPage('/hearing?sid=sess-1')
    // 既存推薦があるので結果ステップへ復元される。
    expect(await screen.findByText('推薦構成')).toBeTruthy()
    expect(screen.getByText('SBA-B')).toBeTruthy()
  })

  it('Q1=other (sample_app null) disables confirm and guides to revise Q1 (no dead-end)', async () => {
    const REC_OTHER = {
      ...REC_SBA_B,
      sample_app: null,
      highlight: null,
      needs_genai_nearest: true,
      genai_nearest_sample_app: 'SBA-C',
      rationale: ['Q1=other: 主 SBA は決定ルールで未定 → 最近傍を GenAI 補助に委ねる'],
    }
    installFetch((u, opts) => {
      if (u.includes('/sessions/sess-x') && (opts?.method ?? 'GET').toUpperCase() === 'GET') {
        return jsonResp({
          id: 'sess-x',
          input_notes: 'その他業務',
          answers: [{ question_id: 'Q1', value: 'other', source: 'sa' }],
          recommendation: REC_OTHER,
        })
      }
      return null
    })
    renderPage('/hearing?sid=sess-x')
    expect(await screen.findByText('推薦構成')).toBeTruthy()
    // 最近傍提案が見え、確定は無効化され、誘導文が出る。
    expect(screen.getByText(/SBA-C/)).toBeTruthy()
    expect((screen.getByRole('button', { name: /この構成で確定/ }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText(/最近傍SBA提案を参考に/)).toBeTruthy()
  })

  it('navigating to a forbidden sid clears prior session state (no stale recommendation)', async () => {
    installFetch((u, opts) => {
      const m = (opts?.method ?? 'GET').toUpperCase()
      if (u.includes('/sessions/sess-ok') && m === 'GET') {
        return jsonResp({
          id: 'sess-ok',
          input_notes: 'メモ',
          answers: [{ question_id: 'Q1', value: 'support', source: 'sa' }],
          recommendation: REC_SBA_B,
        })
      }
      if (u.includes('/sessions/forbidden') && m === 'GET') {
        return jsonResp({ detail: 'hearing session not found' }, 404)
      }
      return null
    })
    function StaleHarness() {
      const [, setSp] = useSearchParams()
      return (
        <>
          <button type="button" onClick={() => setSp({ sid: 'forbidden' })}>
            go-forbidden
          </button>
          <button type="button" onClick={() => setSp({ sid: 'sess-ok' })}>
            go-back
          </button>
        </>
      )
    }
    render(
      <AuthProvider>
        <PrefsProvider>
          <MemoryRouter initialEntries={['/hearing?sid=sess-ok']}>
            <Routes>
              <Route
                path="/hearing"
                element={
                  <>
                    <Hearing />
                    <StaleHarness />
                  </>
                }
              />
            </Routes>
          </MemoryRouter>
        </PrefsProvider>
      </AuthProvider>,
    )
    // 最初のセッションは推薦まで復元される。
    expect(await screen.findByText('推薦構成')).toBeTruthy()
    expect(screen.getByText('SBA-B')).toBeTruthy()
    // forbidden な sid へ遷移 → 旧セッションの推薦が残らずクリアされ、エラー表示。
    fireEvent.click(screen.getByRole('button', { name: 'go-forbidden' }))
    expect(await screen.findByText(/hearing session not found/)).toBeTruthy()
    await waitFor(() => expect(screen.queryByText('推薦構成')).toBeNull())
    expect(screen.queryByText('SBA-B')).toBeNull()
    // 旧セッションのメモも残らない(入力欄が空に初期化される)。
    expect((screen.getByPlaceholderText(/製造業の顧客/) as HTMLTextAreaElement).value).toBe('')

    // 元の sid へ戻ると(クリア済みでも)再取得され復元される。
    fireEvent.click(screen.getByRole('button', { name: 'go-back' }))
    expect(await screen.findByText('推薦構成')).toBeTruthy()
    expect(screen.getByText('SBA-B')).toBeTruthy()
  })

  it('resume with an unknown/forbidden sid (404) surfaces an error and does not restore', async () => {
    // 所有権は API が owner_sub で強制し、他者/不正な sid は 404。UI は握りつぶさず表示する。
    installFetch((u, opts) => {
      if (u.includes('/sessions/other-sid') && (opts?.method ?? 'GET').toUpperCase() === 'GET') {
        return jsonResp({ detail: 'hearing session not found' }, 404)
      }
      return null
    })
    renderPage('/hearing?sid=other-sid')
    expect(await screen.findByText(/hearing session not found/)).toBeTruthy()
    // 他者の回答・推薦は復元されない(入力ステップのまま)。
    expect(screen.queryByText('推薦構成')).toBeNull()
    expect(screen.getByPlaceholderText(/製造業の顧客/)).toBeTruthy()
  })

  it('surfaces a recommend error (e.g. 422 unanswered) without leaving the Q&A step', async () => {
    installFetch((u) => {
      if (u.includes('/sessions/sess-1/recommend') && !u.includes('confirm')) {
        return jsonResp({ detail: '回答必須の質問が未回答: [Q4]' }, 422)
      }
      return null
    })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /提案なしで開始/ }))
    // 全質問を埋めてから確定する(分岐なし)。
    await screen.findByText('この顧客で AI を効かせたい業務は？')
    fireEvent.click(screen.getByRole('radio', { name: /顧客対応\/サポート/ }))
    await settleAfterSelect()
    fireEvent.click(screen.getByRole('button', { name: /次へ/ }))
    fireEvent.click(await screen.findByRole('checkbox', { name: /社内文書/ }))
    await settleAfterSelect()
    fireEvent.click(screen.getByRole('button', { name: /次へ/ }))
    fireEvent.click(await screen.findByRole('radio', { name: /質問に答える/ }))
    await settleAfterSelect()
    fireEvent.click(screen.getByRole('button', { name: /次へ/ }))
    fireEvent.click(await screen.findByRole('radio', { name: /Slack/ }))
    await settleAfterSelect()
    fireEvent.click(screen.getByRole('button', { name: /次へ/ }))
    fireEvent.click(await screen.findByRole('radio', { name: /画面で対話/ }))
    await settleAfterSelect()
    fireEvent.click(screen.getByRole('button', { name: /次へ/ }))
    fireEvent.click(await screen.findByRole('radio', { name: /サンプルシードでOK/ }))
    await settleAfterSelect()
    fireEvent.click(screen.getByRole('button', { name: /確定して推薦/ }))
    expect(await screen.findByText(/回答必須の質問が未回答/)).toBeTruthy()
    // まだ結果ステップへ進んでいない(質問が見えている)。
    expect(screen.getByText('デモ用データはどうする？')).toBeTruthy()
  })
})
