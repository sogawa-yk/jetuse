/** デモ一覧(2026-07-09 施主指示)の UI テスト: 一覧表示・空状態・Open(app-session 一回性コード)・
 *  Delete(確認→DELETE→カード除去)。fetch を URL+メソッドで振り分ける fake にして実描画で検証する。 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthProvider } from '../auth'
import { PrefsProvider } from '../prefs'
import Demos from './demos'

type Route = { method: string; path: string; status?: number; body: unknown }

/** クエリを除く完全一致で振り分け。未登録の呼び出しはテスト失敗にする */
function fakeFetch(routes: Route[]) {
  return vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    const r = routes.find((x) => x.method === method && x.path === url)
    if (!r) throw new Error(`unexpected fetch: ${method} ${url}`)
    return { ok: (r.status ?? 200) < 400, status: r.status ?? 200, json: async () => r.body } as Response
  })
}

function mount() {
  return render(
    <PrefsProvider>
      <AuthProvider>
        <MemoryRouter>
          <Demos />
        </MemoryRouter>
      </AuthProvider>
    </PrefsProvider>,
  )
}

const demo = (over: Record<string, unknown> = {}) => ({
  id: 'd1',
  name: 'サポートデモ',
  description: null,
  status: 'ready',
  created_at: '2026-07-09T00:00:00',
  updated_at: '2026-07-09T00:00:00',
  config: { frontend: { generator: { model: 'gpt-5.6-sol' } } },
  ...over,
})

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('jetuse.lang', 'ja')
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('Demos 一覧', () => {
  it('一覧を表示し、生成モデルとステータスを見せる', async () => {
    vi.stubGlobal('fetch', fakeFetch([{ method: 'GET', path: '/api/demos', body: { demos: [demo()] } }]))
    mount()
    expect(await screen.findByText('サポートデモ')).toBeTruthy()
    expect(screen.getByText('gpt-5.6-sol')).toBeTruthy()
    expect(screen.getByText('準備完了')).toBeTruthy() // ready → demos.st.ready
  })

  it('デモが無ければ空状態を表示する', async () => {
    vi.stubGlobal('fetch', fakeFetch([{ method: 'GET', path: '/api/demos', body: { demos: [] } }]))
    mount()
    expect(await screen.findByText('まだデモがありません')).toBeTruthy()
  })

  it('取得失敗ならエラーメッセージを表示する', async () => {
    vi.stubGlobal('fetch', fakeFetch([{ method: 'GET', path: '/api/demos', status: 500, body: {} }]))
    mount()
    expect(await screen.findByText('デモ一覧の取得に失敗しました。')).toBeTruthy()
  })

  it('「開く」で app-session の一回性コードを添えて新タブで開く', async () => {
    const open = vi.fn()
    vi.stubGlobal('open', open)
    vi.stubGlobal('fetch', fakeFetch([
      { method: 'GET', path: '/api/demos', body: { demos: [demo()] } },
      { method: 'POST', path: '/api/demos/d1/app-session', body: { code: 'abc' } },
    ]))
    mount()
    fireEvent.click(await screen.findByRole('button', { name: /開く/ }))
    await waitFor(() => expect(open).toHaveBeenCalled())
    expect(open.mock.calls[0][0]).toContain('/api/demos/d1/app/?c=abc')
  })

  it('「削除」で確認後 DELETE し、カードを一覧から消す', async () => {
    vi.stubGlobal('confirm', vi.fn(() => true))
    vi.stubGlobal('fetch', fakeFetch([
      { method: 'GET', path: '/api/demos', body: { demos: [demo()] } },
      { method: 'DELETE', path: '/api/demos/d1', body: {} },
    ]))
    mount()
    await screen.findByText('サポートデモ')
    fireEvent.click(screen.getByRole('button', { name: '削除' }))
    await waitFor(() => expect(screen.queryByText('サポートデモ')).toBeNull())
  })
})
