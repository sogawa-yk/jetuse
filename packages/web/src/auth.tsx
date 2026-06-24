/** OIDC認証(Authorization Code + PKCE)。INFRA-02 / specs/06
 *  認証設定は**実行時** `/config.json` から読む(INFRA-03 ORMワンクリック対応)。
 *  config.json は SPAバケットへ Terraform が OIDC client_id 込みで配置する。
 *  `authRequired:true` のとき専用Identity Domainへリダイレクトログイン。
 *  false / 取得失敗時は開発モードで dev-user を返す。
 *
 *  トークン更新(INFRA-02c): 隠しiframeのsilent renewは全廃(5秒無限リトライ+
 *  ループの実機障害)。期限60秒前にトップレベルsigninRedirectを単発実行し、
 *  30秒ガードで自動リダイレクトのループを構造的に防ぐ。 */
/* eslint-disable react-refresh/only-export-components -- contextとhookの同居ファイル */
import { User as OidcUser, UserManager, WebStorageStateStore } from 'oidc-client-ts'
import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'

export type User = { name: string; accessToken?: string }

type RuntimeConfig = { authRequired?: boolean; oidcAuthority?: string; oidcClientId?: string }

// 実行時に loadAuthConfig() で確定する(モジュール読込時点では未確定)。
let authRequired = false
let manager: UserManager | null = null

/** 起動時(render前)に /config.json を読み、認証方式を確定する。
 *  ビルド時の VITE_OIDC_* 依存を排し、Terraform作成のclient_idを実行時に取り込む。 */
export async function loadAuthConfig(): Promise<void> {
  let cfg: RuntimeConfig = {}
  try {
    const res = await fetch('/config.json', { cache: 'no-store' })
    if (res.ok) cfg = (await res.json()) as RuntimeConfig
  } catch {
    /* 取得失敗時は開発モード(dev-user)で続行 */
  }
  authRequired = cfg.authRequired === true && !!cfg.oidcAuthority && !!cfg.oidcClientId
  manager = authRequired
    ? new UserManager({
        authority: cfg.oidcAuthority!,
        client_id: cfg.oidcClientId!,
        redirect_uri: `${window.location.origin}/`,
        post_logout_redirect_uri: `${window.location.origin}/`,
        response_type: 'code',
        scope: 'openid profile email',
        userStore: new WebStorageStateStore({ store: window.sessionStorage }),
        automaticSilentRenew: false, // INFRA-02c: iframe更新は使わない
      })
    : null
}

const AuthContext = createContext<User | null>(null)

// 念のための保険: iframe内に読み込まれてもアプリを起動しない
const inIframe = window.self !== window.top

const REAUTH_AT_KEY = 'jetuse.reauthAt'

/** 自動再ログイン(トップレベルsigninRedirect)。30秒以内の再発火は実行せず
 *  falseを返す(=ループ保険。呼び出し側は手動再ログインUIへ)。 */
function guardedReauth(): boolean {
  if (!manager) return false
  const last = Number(sessionStorage.getItem(REAUTH_AT_KEY) ?? '0')
  if (Date.now() - last < 30_000) return false
  sessionStorage.setItem(REAUTH_AT_KEY, String(Date.now()))
  void manager.signinRedirect()
  return true
}

/** APIが401を返した時の回復(ガード付き)。ガード発動時は何もしない */
export function reauthenticate() {
  guardedReauth()
}

/** ログアウト。認証有効時はIdentity Domainのサインアウトへ。開発モードは再読込のみ */
export function signOut() {
  sessionStorage.removeItem(REAUTH_AT_KEY)
  if (manager) {
    void manager.signoutRedirect()
  } else {
    window.location.assign('/')
  }
}

function toUser(u: OidcUser): User {
  return {
    name: u.profile.name ?? u.profile.sub,
    accessToken: u.access_token,
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(authRequired ? null : { name: 'dev-user' })
  const [error, setError] = useState<string | null>(null)
  const [sessionLost, setSessionLost] = useState(false)

  useEffect(() => {
    const mgr = manager
    if (!mgr || inIframe) return
    const onUserLoaded = (u: OidcUser) => {
      setSessionLost(false)
      setUser(toUser(u))
    }
    // 期限60秒前に単発で再ログイン(セッション生存中は無操作復帰)。
    // ガードに引っかかったら自動遷移せず手動ボタン画面へ(ループ防止)
    const onExpiringOrExpired = () => {
      if (!guardedReauth()) setSessionLost(true)
    }
    mgr.events.addUserLoaded(onUserLoaded)
    mgr.events.addAccessTokenExpiring(onExpiringOrExpired)
    mgr.events.addAccessTokenExpired(onExpiringOrExpired)
    const boot = async () => {
      try {
        const params = new URLSearchParams(window.location.search)
        if (params.has('error')) {
          // 認可エラーリダイレクト → ガード付きで通常ログインへ
          window.history.replaceState({}, '', window.location.pathname)
          if (!guardedReauth()) setSessionLost(true)
          return
        }
        if (params.has('code')) {
          const u = await mgr.signinCallback()
          window.history.replaceState({}, '', window.location.pathname)
          if (u) {
            setUser(toUser(u))
            return
          }
        }
        const u = await mgr.getUser()
        if (u && !u.expired) {
          setUser(toUser(u))
        } else if (!guardedReauth()) {
          setSessionLost(true)
        }
      } catch (e) {
        setError(String(e))
      }
    }
    void boot()
    return () => {
      mgr.events.removeUserLoaded(onUserLoaded)
      mgr.events.removeAccessTokenExpiring(onExpiringOrExpired)
      mgr.events.removeAccessTokenExpired(onExpiringOrExpired)
    }
  }, [])

  if (authRequired && inIframe) return null

  if (error) {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-ink-muted">
        ログインに失敗しました: {error}
      </div>
    )
  }
  if (sessionLost) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-3 text-sm">
        <p>セッションの有効期限が切れました。</p>
        <button
          onClick={() => {
            sessionStorage.removeItem(REAUTH_AT_KEY)
            guardedReauth()
          }}
          className="rounded-rw bg-cta px-4 py-2 text-cta-ink hover:bg-cta-strong"
        >
          再ログイン
        </button>
      </div>
    )
  }
  if (!user) {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-ink-muted">
        サインインしています...
      </div>
    )
  }
  return <AuthContext.Provider value={user}>{children}</AuthContext.Provider>
}

export function useUser(): User {
  const u = useContext(AuthContext)
  if (!u) throw new Error('useUser must be used within AuthProvider')
  return u
}

/** API呼び出し用: Bearerヘッダ(認証無効時は空) */
export function authHeaders(user: User): Record<string, string> {
  return user.accessToken ? { Authorization: `Bearer ${user.accessToken}` } : {}
}
