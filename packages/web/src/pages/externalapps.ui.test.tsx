import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { AuthProvider } from '../auth'
import { PrefsProvider } from '../prefs'
import ExternalApps, { makeNonce, launchLabelKey, type ExternalApp } from './externalapps'

/** external-app 起動導線（ASSET-01 / BE-06）UI ウォークスルー。fetch をモックして
 *  一覧描画 → SSO ハンドオフ取得 → 埋め込み起動（window.open）を操作で通す。実 SSO/exchange は
 *  実 IdP/Vault を要する人間ゲートのため、ここは描画/操作の証跡（mock）。 */

function jsonResp(body: unknown, status = 200) {
  return Promise.resolve({
    ok: status < 400,
    status,
    json: () => Promise.resolve(body),
  } as Response)
}

const APP: ExternalApp = {
  app: 'denpyon',
  embed: 'iframe',
  url: 'https://denpyon.example.com/app',
  title: '伝ぴょん',
  summary: '伝ぴょん連携',
  sso: true,
  source: 'config',
}

function renderPage() {
  return render(
    <PrefsProvider>
      <AuthProvider>
        <ExternalApps />
      </AuthProvider>
    </PrefsProvider>,
  )
}

beforeEach(() => {
  vi.restoreAllMocks()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('externalapps helpers', () => {
  it('makeNonce returns 32 hex chars and is unique', () => {
    const a = makeNonce()
    const b = makeNonce()
    expect(a).toMatch(/^[0-9a-f]{32}$/)
    expect(a).not.toBe(b)
  })

  it('launchLabelKey switches on embed mode', () => {
    expect(launchLabelKey({ ...APP, embed: 'iframe' })).toBe('extapp.embed')
    expect(launchLabelKey({ ...APP, embed: 'link' })).toBe('extapp.open')
  })
})

describe('ExternalApps page', () => {
  it('SSO app: checks sso-launch and shows human-gated notice (no SP-initiated push; no URL open)', async () => {
    const calls: string[] = []
    const open = vi.fn()
    vi.stubGlobal('open', open)
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, init?: RequestInit) => {
        calls.push(`${init?.method ?? 'GET'} ${url}`)
        if (url === '/api/external-apps') return jsonResp({ external_apps: [APP] })
        if (url.endsWith('/sso-launch'))
          return jsonResp({ app: 'denpyon', contains_secret_values: false })
        return jsonResp({}, 404)
      }),
    )

    renderPage()
    expect(await screen.findByText('伝ぴょん')).toBeTruthy()
    fireEvent.click(screen.getByRole('button'))
    // sso-launch で構成は確認するが、handoff code を push せず（login CSRF 回避。BE06-BLK-003）、
    // 未認証 URL は開かず・埋め込まず、人間ゲート通知を出す（BE06-001。安全な起点は RP 側）。
    expect(await screen.findByText(/実 SSO 設定|real SSO setup/)).toBeTruthy()
    expect(document.querySelector('iframe')).toBeNull()
    expect(open).not.toHaveBeenCalled()
    expect(calls.some((c) => c.startsWith('POST') && c.endsWith('/sso-launch'))).toBe(true)
  })

  it('non-SSO iframe app embeds inline', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url === '/api/external-apps')
          return jsonResp({ external_apps: [{ ...APP, sso: false }] })
        return jsonResp({}, 404)
      }),
    )
    renderPage()
    fireEvent.click(await screen.findByRole('button'))
    await waitFor(() => {
      const iframe = document.querySelector('iframe')
      expect(iframe?.getAttribute('src')).toBe('https://denpyon.example.com/app')
    })
  })

  it('opens non-SSO link-embed app in a new tab', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url === '/api/external-apps')
          return jsonResp({ external_apps: [{ ...APP, embed: 'link', sso: false }] })
        return jsonResp({}, 404)
      }),
    )
    const open = vi.fn()
    vi.stubGlobal('open', open)
    renderPage()
    fireEvent.click(await screen.findByRole('button'))
    await waitFor(() =>
      expect(open).toHaveBeenCalledWith(
        'https://denpyon.example.com/app',
        '_blank',
        'noopener,noreferrer',
      ),
    )
  })

  it('shows empty state when no apps configured', async () => {
    vi.stubGlobal('fetch', vi.fn(() => jsonResp({ external_apps: [] })))
    renderPage()
    expect(await screen.findByText(/構成済みの外部アプリがありません|No configured/)).toBeTruthy()
  })

  it('surfaces handoff error from sso-launch (fail-closed)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url === '/api/external-apps') return jsonResp({ external_apps: [APP] })
        return jsonResp({ detail: 'claim missing' }, 422)
      }),
    )
    vi.stubGlobal('open', vi.fn())
    renderPage()
    fireEvent.click(await screen.findByRole('button'))
    expect(await screen.findByText('claim missing')).toBeTruthy()
  })
})
