import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { AuthProvider } from '../auth'
import { PrefsProvider } from '../prefs'
import Grants from './grants'

/** BE-05 UI ウォークスルー(実 <Grants/> をレンダリングし fetch をモックして、候補→スコープ選択→承認→
 *  一覧反映→失効 を操作で通す)。実機 E2E(HTTP フルスタック)は runs/<rid>/e2e/ が担い、ここは描画/操作の証跡。 */

function jsonResp(body: unknown, status = 200) {
  return Promise.resolve({
    ok: status < 400,
    status,
    json: () => Promise.resolve(body),
  } as Response)
}

const CANDIDATE = {
  plugin_id: 'acme/faq',
  version: '1.2.0',
  name: 'FAQ要約',
  declared_scopes: ['platform:db.query', 'platform:rag.search'],
}

let grants: unknown[]

beforeEach(() => {
  grants = []
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, opts?: RequestInit) => {
      const u = String(url)
      const method = opts?.method ?? 'GET'
      if (u.endsWith('/api/platform/grants/candidates')) {
        return jsonResp({ candidates: [CANDIDATE] })
      }
      if (u.endsWith('/api/platform/grants') && method === 'GET') {
        return jsonResp({ grants })
      }
      if (u.endsWith('/api/platform/grants') && method === 'POST') {
        const body = JSON.parse(String(opts?.body))
        grants = [
          {
            id: 'g1',
            tenant: body.tenant,
            plugin_id: body.plugin_id,
            source_version: '1.2.0',
            scopes: body.scopes,
            status: 'ACTIVE',
            approved_by: 'dev-user',
            updated_at: '2026-06-29T00:00:00Z',
          },
        ]
        return jsonResp({ ...(grants[0] as object) })
      }
      if (u.endsWith('/api/platform/grants') && method === 'DELETE') {
        grants = []
        return jsonResp({ revoked: true })
      }
      return jsonResp({})
    }),
  )
})

afterEach(() => vi.unstubAllGlobals())

function renderPage() {
  return render(
    <AuthProvider>
      <PrefsProvider>
        <Grants />
      </PrefsProvider>
    </AuthProvider>,
  )
}

describe('Grants UI (BE-05)', () => {
  it('承認フォームで scope を選び承認すると一覧に反映、失効で消える', async () => {
    renderPage()
    // 候補が select に出る。
    const select = await screen.findByRole('combobox')
    fireEvent.change(select, { target: { value: 'acme/faq' } })

    // 宣言スコープがチェックボックスとして全選択される。
    const boxes = await screen.findAllByRole('checkbox')
    expect(boxes).toHaveLength(2)
    expect((boxes[0] as HTMLInputElement).checked).toBe(true)

    // tenant を入力。
    fireEvent.change(screen.getByPlaceholderText(/ocid1.tenancy/), {
      target: { value: 'ocid1.tenancy.oc1..aaaa' },
    })

    // 承認。
    fireEvent.click(screen.getByRole('button', { name: '承認' }))

    // 一覧に承認済みグラントが現れる。
    await waitFor(() => expect(screen.getByText('acme/faq@1.2.0')).toBeInTheDocument())
    expect(screen.getByText('ACTIVE')).toBeInTheDocument()

    // 失効。
    fireEvent.click(screen.getByRole('button', { name: '失効' }))
    await waitFor(() =>
      expect(screen.queryByText('acme/faq@1.2.0')).not.toBeInTheDocument(),
    )
  })

  it('403 は forbidden 表示に倒す', async () => {
    vi.stubGlobal('fetch', vi.fn(() => jsonResp({ detail: 'forbidden' }, 403)))
    renderPage()
    await waitFor(() =>
      expect(screen.getByText(/管理者（ADMIN_USERS）のみ/)).toBeInTheDocument(),
    )
  })
})
