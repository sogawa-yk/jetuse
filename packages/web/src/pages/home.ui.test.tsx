import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { AuthProvider } from '../auth'
import { PrefsProvider } from '../prefs'
import Home from './home'

/** demo-quality 回帰(SBA-02): /api/usecases が DB ダウン時に 503 {detail} を返しても
 *  Home がクラッシュせず(旧: usecases=undefined → flatMap で TypeError)、業務アプリデモ
 *  (SBA-A)導線が表示され続けることを検証する。 */

function jsonResp(body: unknown, status = 200) {
  return Promise.resolve({
    ok: status < 400,
    status,
    json: () => Promise.resolve(body),
  } as Response)
}

function mockFetch(usecasesResp: () => Promise<Response>) {
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) => {
      const u = String(url)
      if (u.includes('/api/usecases')) return usecasesResp()
      if (u.includes('/api/agents')) return jsonResp({ agents: [] })
      if (u.includes('/api/sample-apps')) {
        return jsonResp({
          sample_apps: [
            {
              id: 'builtin-sba-a',
              name: '問い合わせ/サポート管理',
              description: 'FAQ-RAG デモ',
              icon: '💬',
              capabilities: ['rag.search'],
            },
          ],
        })
      }
      return jsonResp({})
    }),
  )
}

afterEach(() => vi.unstubAllGlobals())

function renderHome() {
  return render(
    <AuthProvider>
      <PrefsProvider>
        <MemoryRouter>
          <Home />
        </MemoryRouter>
      </PrefsProvider>
    </AuthProvider>,
  )
}

describe('Home resilience to API failures', () => {
  it('does not crash and shows SBA-A when /api/usecases returns 503 {detail}', async () => {
    // DB ダウン: 503 + 配列でない detail オブジェクト(旧実装が落ちたケース)。
    mockFetch(() => jsonResp({ detail: 'database unavailable' }, 503))
    renderHome()
    // SBA-A の業務アプリデモ導線が表示される(クラッシュしていない)。
    expect(await screen.findByText('問い合わせ/サポート管理')).toBeTruthy()
  })

  it('does not crash when /api/usecases returns a non-array body on 200', async () => {
    mockFetch(() => jsonResp({ usecases: { not: 'an array' } }))
    renderHome()
    expect(await screen.findByText('問い合わせ/サポート管理')).toBeTruthy()
  })

  it('shows SBA-A link pointing to /sba/:id', async () => {
    mockFetch(() => jsonResp({ usecases: [] }))
    renderHome()
    const link = (await screen.findByText('問い合わせ/サポート管理')).closest('a')
    await waitFor(() => expect(link?.getAttribute('href')).toContain('/sba/builtin-sba-a'))
  })
})
