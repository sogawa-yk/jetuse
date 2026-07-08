/** デモビルダー(SP3-05)の UI ロジックテスト: 状態遷移・エラー表示・failed → 再生成導線。
 *  fetch を URL+メソッドで振り分ける fake にし、実コンポーネントを描画して検証する。 */
import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthProvider } from '../../auth'
import { PrefsProvider } from '../../prefs'
import DemoBuilder from './index'
import { SID_KEY, type Session } from './state'

const PLAN = {
  plan_version: 1,
  title: '保全デモ',
  description: 'マニュアル検索デモ',
  capabilities: ['chat', 'rag.search'],
  screens: [
    {
      id: 'home',
      title: 'ホーム',
      blocks: [{ type: 'chat', title: 'アシスタント' }],
    },
  ],
  data: { documents: [{ filename: 'manual.md', title: 'マニュアル', outline: '章立て' }] },
}

const SESSION: Session = {
  id: 'sid-1',
  status: 'hearing',
  transcript: [],
  requirements: null,
  plan: null,
  demo_id: null,
  demo_status: null,
  created_at: '2026-07-08T00:00:00',
  updated_at: '2026-07-08T00:00:00',
}

type Route = { method: string; path: string; status?: number; body: unknown }

/** 先頭一致でなく完全一致(クエリ除く)。未登録の呼び出しはテスト失敗にする */
function fakeFetch(routes: Route[]) {
  return vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    const r = routes.find((x) => x.method === method && x.path === url)
    if (!r) throw new Error(`unexpected fetch: ${method} ${url}`)
    return {
      ok: (r.status ?? 200) < 400,
      status: r.status ?? 200,
      json: async () => r.body,
    } as Response
  })
}

function mount() {
  return render(
    <PrefsProvider>
      <AuthProvider>
        <DemoBuilder />
      </AuthProvider>
    </PrefsProvider>,
  )
}

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('jetuse.lang', 'ja')
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('ヒアリング(①) — 充足可視化と設計ゲートの状態遷移', () => {
  it('sufficient=false の間は「設計へ」が無効で、missing が表示される', async () => {
    const fetchMock = fakeFetch([
      { method: 'POST', path: '/api/builder/sessions', body: SESSION },
      {
        method: 'POST',
        path: '/api/builder/sessions/sid-1/messages',
        body: {
          reply: '業種を教えてください',
          requirements: { use_case: '検索デモ' },
          sufficient: false,
          missing: ['industry'],
        },
      },
    ])
    vi.stubGlobal('fetch', fetchMock)
    mount()

    fireEvent.change(screen.getByPlaceholderText(/例:/), { target: { value: '検索デモを作りたい' } })
    fireEvent.click(screen.getByRole('button', { name: '送信' }))

    await screen.findByText('業種を教えてください')
    expect(screen.getByText(/不足:/)).toHaveTextContent('industry')
    expect(screen.getByRole('button', { name: '設計へ' })).toBeDisabled()
    // セッション id が localStorage に保存される(直近セッション復帰 — §7)
    expect(localStorage.getItem(SID_KEY)).toBe('sid-1')
  })

  it('sufficient=true で「設計へ」が有効になり、design 成功でプラン確認(②)へ進む', async () => {
    const designed: Session = {
      ...SESSION,
      status: 'designed',
      plan: PLAN,
      requirements: {
        industry: '製造',
        use_case: '検索',
        data_profile: { documents: 'マニュアル' },
      },
    }
    const fetchMock = fakeFetch([
      { method: 'POST', path: '/api/builder/sessions', body: SESSION },
      {
        method: 'POST',
        path: '/api/builder/sessions/sid-1/messages',
        body: {
          reply: '要件は揃いました',
          requirements: designed.requirements,
          sufficient: true,
          missing: [],
        },
      },
      { method: 'POST', path: '/api/builder/sessions/sid-1/design', body: designed },
    ])
    vi.stubGlobal('fetch', fetchMock)
    mount()

    fireEvent.change(screen.getByPlaceholderText(/例:/), {
      target: { value: '製造業のマニュアル検索デモ' },
    })
    fireEvent.click(screen.getByRole('button', { name: '送信' }))
    await screen.findByText('要件は揃いました')

    const toDesign = screen.getByRole('button', { name: '設計へ' })
    expect(toDesign).toBeEnabled()
    fireEvent.click(toDesign)

    // ②: プラン要約(能力チップ・画面構成)と title/description の直接編集欄のみ
    await screen.findByText('使用する能力')
    expect(screen.getByText('rag.search')).toBeInTheDocument()
    expect(screen.getByDisplayValue('保全デモ')).toBeInTheDocument()
    expect(screen.getByDisplayValue('マニュアル検索デモ')).toBeInTheDocument()
  })
})

describe('エラー表示', () => {
  it('design の 409 は detail をそのまま通知に出す', async () => {
    const fetchMock = fakeFetch([
      { method: 'POST', path: '/api/builder/sessions', body: SESSION },
      {
        method: 'POST',
        path: '/api/builder/sessions/sid-1/messages',
        body: { reply: 'ok', requirements: {}, sufficient: true, missing: [] },
      },
      {
        method: 'POST',
        path: '/api/builder/sessions/sid-1/design',
        status: 409,
        body: { detail: '要求サマリが設計に足りません(missing: industry)' },
      },
    ])
    vi.stubGlobal('fetch', fetchMock)
    mount()

    fireEvent.change(screen.getByPlaceholderText(/例:/), { target: { value: 'デモ' } })
    fireEvent.click(screen.getByRole('button', { name: '送信' }))
    await screen.findByText('ok')
    fireEvent.click(screen.getByRole('button', { name: '設計へ' }))

    await screen.findByText(/要求サマリが設計に足りません/)
  })
})

describe('生成(③) — failed の理由表示と再生成導線', () => {
  const failedSession: Session = {
    ...SESSION,
    status: 'designed',
    plan: PLAN,
    demo_id: 'd1',
    demo_status: 'failed',
  }
  const failedDemo = {
    id: 'd1',
    name: '保全デモ',
    description: null,
    status: 'failed',
    config: { generation: { error: '静的検査に不合格(スコープ外参照)' } },
  }

  it('復帰した failed セッションで理由と「再生成」を表示し、クリックで generate が走る', async () => {
    localStorage.setItem(SID_KEY, 'sid-1')
    const fetchMock = fakeFetch([
      { method: 'GET', path: '/api/builder/sessions/sid-1', body: failedSession },
      { method: 'GET', path: '/api/demos/d1', body: failedDemo },
      { method: 'POST', path: '/api/builder/sessions/sid-1/generate', body: { demo_id: 'd1' } },
    ])
    vi.stubGlobal('fetch', fetchMock)
    mount()

    // failed: 理由(config.generation.error)+再生成ボタン
    await screen.findByText(/生成に失敗しました/)
    expect(screen.getByText(/静的検査に不合格/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /再生成/ }))
    // 202 受理 → 生成進行(ポーリング)表示へ戻る
    await screen.findByText(/デモを生成しています/)
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          url === '/api/builder/sessions/sid-1/generate' && init?.method === 'POST',
      ),
    ).toBe(true)
  })

  it('ready まで進むと確定(⑤)の PATCH → 完了表示、localStorage が消える', async () => {
    const readySession: Session = { ...failedSession, demo_status: 'ready' }
    const readyDemo = { ...failedDemo, status: 'ready', description: '説明' }
    const fetchMock = fakeFetch([
      { method: 'GET', path: '/api/builder/sessions/sid-1', body: readySession },
      { method: 'GET', path: '/api/demos/d1', body: readyDemo },
      { method: 'PATCH', path: '/api/demos/d1', body: readyDemo },
    ])
    localStorage.setItem(SID_KEY, 'sid-1')
    vi.stubGlobal('fetch', fetchMock)
    mount()

    // ④ プレビュー画面 → 確定へ
    await screen.findByText(/生成が完了しました/)
    fireEvent.click(screen.getByRole('button', { name: /確定へ/ }))

    // ⑤ 確定フォーム(name プリフィル)→ 保存
    const name = await screen.findByDisplayValue('保全デモ')
    fireEvent.change(name, { target: { value: '完成版デモ' } })
    fireEvent.click(screen.getByRole('button', { name: '確定して保存' }))

    await screen.findByText('デモを保存しました')
    expect(localStorage.getItem(SID_KEY)).toBeNull()
    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === 'PATCH')
    expect(patch).toBeTruthy()
    expect(JSON.parse(String(patch![1]!.body))).toEqual({
      name: '完成版デモ',
      description: '説明',
    })
  })

  it('破棄は確認ダイアログ付きで DELETE し、①へ戻る', async () => {
    const readySession: Session = { ...failedSession, demo_status: 'ready' }
    const readyDemo = { ...failedDemo, status: 'ready' }
    const fetchMock = fakeFetch([
      { method: 'GET', path: '/api/builder/sessions/sid-1', body: readySession },
      { method: 'GET', path: '/api/demos/d1', body: readyDemo },
      { method: 'DELETE', path: '/api/demos/d1', body: { deleted: true } },
    ])
    localStorage.setItem(SID_KEY, 'sid-1')
    vi.stubGlobal('fetch', fetchMock)
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    mount()

    await screen.findByText(/生成が完了しました/)
    fireEvent.click(screen.getByRole('button', { name: 'このデモを破棄' }))

    await screen.findByText('作りたいデモを教えてください')
    expect(confirmSpy).toHaveBeenCalled()
    expect(localStorage.getItem(SID_KEY)).toBeNull()
  })
})
