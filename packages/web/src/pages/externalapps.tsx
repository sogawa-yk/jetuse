/* eslint-disable react-refresh/only-export-components -- 純粋ヘルパーを単体テスト用に同居 */
import { useEffect, useState } from 'react'
import { authHeaders, useUser } from '../auth'
import { PageContainer } from '../components/layout'
import { usePrefs } from '../prefs'

/** 外部アプリ連携（external-app / ASSET-01・BE-06）の起動導線。
 *  /api/external-apps で構成済み・install 済みの external-app を一覧し、SSO ハンドオフ（sso-launch）を
 *  取得して埋め込み先 URL を開く。**実 token-exchange（sso-exchange）は実 IdP/Vault が要る人間ゲート**
 *  のため、この導線は決定的・オフラインの handoff shape 取得＋埋め込み起動までを担う。 */

export type ExternalApp = {
  app: string
  embed: 'iframe' | 'link'
  url: string
  title: string
  summary?: string
  sso: boolean
  source?: string
}

// --- 純粋ヘルパ（単体テスト対象） ---

/** front-channel の state/nonce（CSRF/リプレイ対策値）を生成する。 */
export function makeNonce(): string {
  const buf = new Uint8Array(16)
  crypto.getRandomValues(buf)
  return Array.from(buf, (b) => b.toString(16).padStart(2, '0')).join('')
}

/** 起動ボタンの i18n キー（埋め込み方式で出し分け）。呼び出し側で t() に渡す。 */
export function launchLabelKey(app: ExternalApp): 'extapp.embed' | 'extapp.open' {
  return app.embed === 'iframe' ? 'extapp.embed' : 'extapp.open'
}

// --- ページ ---

export default function ExternalApps() {
  const { t } = usePrefs()
  const user = useUser()
  const [apps, setApps] = useState<ExternalApp[]>([])
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  // iframe 埋め込み中のアプリ（embed=iframe 起動時に画面内へ枠表示する）。
  const [embedded, setEmbedded] = useState<ExternalApp | null>(null)
  // SSO アプリで起動可だが、ログイン確立に実 SSO 設定（人間ゲート）が要る旨の通知。
  const [ssoGated, setSsoGated] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    fetch('/api/external-apps', { headers: authHeaders(user) })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((body) => {
        if (!active) return
        setApps(body.external_apps ?? [])
        setStatus('ready')
      })
      .catch(() => {
        if (active) setStatus('error')
      })
    return () => {
      active = false
    }
  }, [user])

  async function launch(app: ExternalApp) {
    setBusy(app.app)
    setError(null)
    setSsoGated(null)
    try {
      if (app.sso) {
        // SSO ハンドオフ shape（sso-launch）を取得して SSO 構成・claim 充足を確認する（決定的・参照名のみ・
        // 実トークン非保持）。**安全なログイン確立は RP（連携先アプリ）起点のフロー**にする必要がある:
        // 連携先が state/PKCE を生成して JetUse へ誘導 → JetUse が実 token-exchange し handoff code に束ね
        // （id_token はブラウザに渡さない。BE06-SSO-002）→ 連携先がバックチャネル sso-redeem で交換し、
        // 自身が生成した state を検証してセッション確立する（認可コード型。サーバ側 sso-exchange/sso-redeem
        // で実装済み）。**JetUse 側から handoff code を push する SP 起点フローは login CSRF を招く**ため
        // 行わない（BE06-BLK-003）。実 IdP/Vault＋連携先の起点実装は人間ゲートなので、ここでは起動可否のみ
        // 確認し、未認証 URL を「ログイン済み」と偽らずに（BE06-001）人間ゲートである旨を通知する。
        const res = await fetch(`/api/external-apps/${app.app}/sso-launch`, {
          method: 'POST',
          headers: { 'content-type': 'application/json', ...authHeaders(user) },
          body: JSON.stringify({ state: makeNonce(), nonce: makeNonce() }),
        })
        if (!res.ok) {
          const body = await res.json().catch(() => ({}))
          throw new Error(body.detail || `HTTP ${res.status}`)
        }
        setSsoGated(app.app)
        return
      }
      // 非 SSO（ログイン不要）アプリは embed 方式どおり起動する: iframe=枠埋め込み / link=別タブ。
      if (app.embed === 'iframe') {
        setEmbedded(app)
      } else {
        window.open(app.url, '_blank', 'noopener,noreferrer')
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  return (
    <PageContainer icon="market" title={t('extapp.title')} subtitle={t('extapp.subtitle')}>
      {status === 'loading' && <p className="text-sm text-slate-500">{t('extapp.loading')}</p>}
      {status === 'error' && <p className="text-sm text-rose-600">{t('extapp.error')}</p>}
      {status === 'ready' && apps.length === 0 && (
        <p className="text-sm text-slate-500">{t('extapp.empty')}</p>
      )}
      {status === 'ready' && apps.length > 0 && (
        <ul className="space-y-3">
          {apps.map((a) => (
            <li
              key={a.app}
              className="flex items-center justify-between rounded-lg border border-slate-200 p-4"
            >
              <div className="min-w-0">
                <div className="font-medium">{a.title}</div>
                {a.summary && <div className="truncate text-sm text-slate-500">{a.summary}</div>}
                <div className="mt-1 text-xs text-slate-400">
                  {a.embed}
                  {a.sso ? ' · SSO' : ''}
                  {a.source ? ` · ${a.source}` : ''}
                </div>
              </div>
              <button
                type="button"
                className="ml-4 shrink-0 rounded-md bg-sky-600 px-3 py-1.5 text-sm text-white disabled:opacity-50"
                disabled={busy === a.app}
                onClick={() => launch(a)}
              >
                {t(launchLabelKey(a))}
              </button>
            </li>
          ))}
        </ul>
      )}
      {error && <p className="mt-3 text-sm text-rose-600">{error}</p>}
      {ssoGated && <p className="mt-3 text-sm text-amber-600">{t('extapp.ssoGated')}</p>}
      {embedded && (
        <div className="mt-4">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-sm font-medium">{embedded.title}</span>
            <button
              type="button"
              className="rounded-md border border-slate-300 px-2 py-1 text-xs"
              onClick={() => setEmbedded(null)}
            >
              ×
            </button>
          </div>
          <iframe
            title={embedded.title}
            src={embedded.url}
            className="h-[70vh] w-full rounded-lg border border-slate-200"
            sandbox="allow-scripts allow-forms allow-same-origin allow-popups"
          />
        </div>
      )}
    </PageContainer>
  )
}
