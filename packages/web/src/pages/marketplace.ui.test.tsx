import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { AuthProvider } from '../auth'
import { PrefsProvider } from '../prefs'
import Marketplace from './marketplace'

/** PLG-06 UI ウォークスルー(実 <Marketplace/> をレンダリングし、fetch をモックして
 *  一覧 → 詳細 → install → インストール済み反映 → uninstall を操作で通す)。
 *  実機 E2E(HTTP フルスタック)は runs/<rid>/e2e/e2e_marketplace.py が担い、ここは描画/操作の証跡。 */

function jsonResp(body: unknown, status = 200) {
  return Promise.resolve({
    ok: status < 400,
    status,
    json: () => Promise.resolve(body),
  } as Response)
}

function card(installed: boolean) {
  return {
    id: 'acme/faq',
    version: '1.2.0',
    kind: 'usecase',
    name: 'FAQ要約',
    description: 'FAQを要約する',
    publisher: 'acme-corp',
    tags: ['faq'],
    versions: ['1.2.0'],
    installed,
    installed_versions: installed ? ['1.2.0'] : [],
    uninstallable_versions: installed ? ['1.2.0'] : [],
    update_available: false,
    installable: true,
    can_uninstall: installed,
  }
}

let installed = false

beforeEach(() => {
  installed = false
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, opts?: RequestInit) => {
      const u = String(url)
      const method = opts?.method ?? 'GET'
      if (u.includes('/api/marketplace/plugins/acme/faq')) {
        return jsonResp({ ...card(installed), permissions: ['platform:rag.search'], signed: true })
      }
      if (u.endsWith('/api/marketplace/plugins')) {
        return jsonResp({ plugins: [card(installed)], tags: ['faq'] })
      }
      if (u.endsWith('/api/marketplace/install') && method === 'POST') {
        installed = true
        return jsonResp({ installed: true })
      }
      if (u.endsWith('/api/marketplace/uninstall') && method === 'POST') {
        installed = false
        return jsonResp({ uninstalled: true })
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
        <Marketplace />
      </PrefsProvider>
    </AuthProvider>,
  )
}

describe('Marketplace UI walkthrough (PLG-06)', () => {
  it('lists, opens detail, installs, reflects installed, then uninstalls', async () => {
    renderPage()

    // 1) 一覧: カードが出る
    const cardBtn = await screen.findByRole('button', { name: /FAQ要約/ })
    expect(cardBtn).toBeTruthy()

    // 2) 詳細: カードを選ぶと permissions と install ボタンが出る
    fireEvent.click(cardBtn)
    const aside = await screen.findByRole('complementary')
    await within(aside).findByText(/platform:rag\.search/)
    const installBtn = within(aside).getByRole('button', { name: 'インストール' })
    expect(installBtn).toBeTruthy()

    // 3) install → インストール済みバッジが反映される
    fireEvent.click(installBtn)
    await waitFor(() => {
      const a = screen.getByRole('complementary')
      expect(within(a).getAllByText(/インストール済み/).length).toBeGreaterThan(0)
    })

    // 4) uninstall → ボタンが出て押せる(押すと未インストールへ戻る)
    const uninstallBtn = within(screen.getByRole('complementary')).getByRole('button', {
      name: 'アンインストール',
    })
    fireEvent.click(uninstallBtn)
    await waitFor(() => {
      const a = screen.getByRole('complementary')
      expect(within(a).getByRole('button', { name: 'インストール' })).toBeTruthy()
    })
  })
})
