/** OCIコンソール風シェル(UI-02 / docs/ui/screen-example-*.png 準拠):
 *  ダークヘッダ(neutral-170 #312D2A・中央検索バー・リージョン表示) + 白い左ナビ
 *  + 白いページヘッダ + ライトグレー本文 + ダークフッター */
import {
  useEffect, useRef, useState,
  type PointerEvent as ReactPointerEvent, type ReactNode,
} from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import { authHeaders, signOut, useUser } from '../auth'
import type { Branding } from '../branding'
import { usePrefs } from '../prefs'
import logoMark from '../assets/theo_white.png'
import { NavIcon, isIconName, type IconName } from './icons'
import { HelpMark } from './HelpMark'
import type { HelpKey } from './helpContent'

type Me = { name: string; email: string | null; is_admin: boolean }

const NAV: { to: string; key: string; icon: IconName }[] = [
  { to: '/', key: 'nav.home', icon: 'home' },
  { to: '/chat', key: 'nav.chat', icon: 'chat' },
  { to: '/agents', key: 'nav.agents', icon: 'agents' },
  { to: '/rag', key: 'nav.rag', icon: 'rag' },
  { to: '/dbchat', key: 'nav.dbchat', icon: 'dbchat' },
  { to: '/minutes', key: 'nav.minutes', icon: 'minutes' },
  { to: '/realtime', key: 'nav.realtime', icon: 'realtime' },
  { to: '/voicechat', key: 'nav.voicechat', icon: 'voicechat' },
  { to: '/video', key: 'nav.video', icon: 'video' },
  { to: '/ocr', key: 'nav.ocr', icon: 'ocr' },
  { to: '/demo-builder', key: 'nav.demobuilder', icon: 'idea' },
  { to: '/admin', key: 'nav.admin', icon: 'admin' },
  { to: '/settings', key: 'nav.settings', icon: 'settings' },
  { to: '/design', key: 'nav.design', icon: 'design' },
]

const isDesktop = () => window.matchMedia('(min-width: 768px)').matches

// 左ナビ幅(デスクトップのみ可変・この端末に永続化。feedback 20260618-3 #3)
const NAV_WIDTH_KEY = 'jetuse.navWidth'
const NAV_MIN = 180
const NAV_MAX = 420
const loadNavWidth = (): number => {
  const v = Number(localStorage.getItem(NAV_WIDTH_KEY))
  return v >= NAV_MIN && v <= NAV_MAX ? v : 224
}

export function Shell({ branding }: { branding: Branding | null }) {
  // モバイルは初期クローズ+オーバーレイドロワー、デスクトップは常設
  const [navOpen, setNavOpen] = useState(isDesktop)
  const [desktop, setDesktop] = useState(isDesktop)
  const [navWidth, setNavWidth] = useState(loadNavWidth)
  const navWidthRef = useRef(navWidth)
  const [me, setMe] = useState<Me | null>(null)
  const [accountOpen, setAccountOpen] = useState(false)
  const accountRef = useRef<HTMLDivElement>(null)
  const { t, lang, setLang, dark, setDark } = usePrefs()
  const user = useUser()
  const closeOnMobile = () => {
    if (!isDesktop()) setNavOpen(false)
  }

  // 画面幅の変化に追従(可変幅はデスクトップ時のみ適用するため)
  useEffect(() => {
    const onResize = () => setDesktop(isDesktop())
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  // ナビ右端ハンドルのドラッグで幅を変更(離した時に永続化)
  const startResize = (e: ReactPointerEvent) => {
    e.preventDefault()
    const startX = e.clientX
    const startW = navWidthRef.current
    const onMove = (ev: PointerEvent) => {
      const w = Math.min(NAV_MAX, Math.max(NAV_MIN, startW + ev.clientX - startX))
      navWidthRef.current = w
      setNavWidth(w)
    }
    const onUp = () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      document.body.style.userSelect = ''
      localStorage.setItem(NAV_WIDTH_KEY, String(navWidthRef.current))
    }
    document.body.style.userSelect = 'none'
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
  }

  useEffect(() => {
    fetch('/api/me', { headers: authHeaders(user) })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setMe(d))
      .catch(() => setMe(null))
  }, [user])

  // アカウントメニューの外側クリックで閉じる
  useEffect(() => {
    if (!accountOpen) return
    const onClick = (e: MouseEvent) => {
      if (accountRef.current && !accountRef.current.contains(e.target as Node)) {
        setAccountOpen(false)
      }
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [accountOpen])

  // 権限のないユーザーには管理メニューを表示しない
  const navItems = NAV.filter((it) => it.to !== '/admin' || me?.is_admin)

  return (
    <div className="flex h-screen flex-col overflow-hidden">
      {/* ヘッダ(neutral-170・中央に検索バー・右にリージョン/アバター) */}
      <header className="sticky top-0 z-20 flex h-[53px] items-center gap-2.5 bg-header px-2.5 text-header-ink sm:px-3">
        <button
          aria-label="toggle navigation"
          onClick={() => setNavOpen(!navOpen)}
          className="rounded-rw px-2 py-1.5 text-xl leading-none hover:bg-header-ink/10"
        >
          ☰
        </button>
        <img src={logoMark} alt="" className="h-9 w-9 shrink-0 object-contain" />
        <span className="min-w-0 truncate text-base font-medium">
          {branding?.shortName ?? '...'}
        </span>

        <div className="ml-auto flex shrink-0 items-center gap-1.5 text-sm">
          <button
            onClick={() => setLang(lang === 'ja' ? 'en' : 'ja')}
            className="rounded-rw px-2.5 py-1.5 hover:bg-header-ink/10"
            title={t('settings.lang')}
          >
            {lang === 'ja' ? 'EN' : '日本語'}
          </button>
          <button
            onClick={() => setDark(!dark)}
            className="rounded-rw px-2.5 py-1.5 hover:bg-header-ink/10"
            title={t('settings.theme')}
          >
            {dark ? '☀' : '☾'}
          </button>
          {/* アカウントメニュー(ユーザー情報 + ログアウト) */}
          <div className="relative ml-1" ref={accountRef}>
            <button
              onClick={() => setAccountOpen((v) => !v)}
              aria-label={t('account.menu')}
              aria-haspopup="menu"
              aria-expanded={accountOpen}
              className="flex h-9 w-9 items-center justify-center rounded-full bg-band-chip text-sm font-semibold text-white hover:opacity-90"
            >
              {(me?.name ?? user.name).charAt(0).toUpperCase()}
            </button>
            {accountOpen && (
              <div
                role="menu"
                className="absolute right-0 top-11 z-40 w-60 overflow-hidden rounded-rw border border-line bg-surface text-ink shadow-lg"
              >
                <div className="border-b border-line px-4 py-3">
                  <p className="truncate text-sm font-semibold">{me?.name ?? user.name}</p>
                  {me?.email && <p className="truncate text-xs text-ink-muted">{me.email}</p>}
                  {me?.is_admin && (
                    <span className="mt-1 inline-block rounded-full bg-action-soft px-2 py-0.5 text-[10px] text-ink">
                      {t('account.admin')}
                    </span>
                  )}
                </div>
                <button
                  onClick={signOut}
                  role="menuitem"
                  className="block w-full px-4 py-2.5 text-left text-sm text-ink hover:bg-ink/8"
                >
                  {t('account.logout')}
                </button>
              </div>
            )}
          </div>
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        {/* 左ナビ(白面・選択はライトブルーのハイライト)。モバイルはオーバーレイドロワー */}
        {navOpen && (
          <>
            <div
              className="fixed inset-x-0 bottom-0 top-[53px] z-30 bg-header/30 md:hidden"
              onClick={() => setNavOpen(false)}
            />
            {/* サイドバーは灰色面(screen-example-2準拠)。デスクトップは可変幅 */}
            <nav
              style={desktop ? { width: navWidth } : undefined}
              className="w-56 shrink-0 border-r border-line bg-bg max-md:fixed max-md:bottom-0 max-md:left-0 max-md:top-[53px] max-md:z-40 max-md:overflow-y-auto max-md:shadow-lg"
            >
              <ul className="space-y-0.5 p-2">
              {navItems.map((it) => (
                <li key={it.to}>
                  <NavLink
                    to={it.to}
                    end={it.to === '/'}
                    onClick={closeOnMobile}
                    className={({ isActive }) =>
                      `flex w-full items-center gap-2.5 rounded-rw px-3 py-2 text-sm transition-colors ${
                        isActive
                          ? 'bg-action-soft font-semibold text-ink'
                          : 'text-ink-muted hover:bg-ink/8 hover:text-ink'
                      }`
                    }
                  >
                    <NavIcon name={it.icon} className="h-[18px] w-[18px] shrink-0" />
                    {t(it.key as Parameters<typeof t>[0])}
                  </NavLink>
                </li>
              ))}
              </ul>
            </nav>
            {/* 幅調整ハンドル(デスクトップのみ。ドラッグで左ナビ幅を変更) */}
            {desktop && (
              <div
                onPointerDown={startResize}
                role="separator"
                aria-orientation="vertical"
                aria-label={t('nav.resize')}
                title={t('nav.resize')}
                className="w-1 shrink-0 cursor-col-resize bg-line/50 transition-colors hover:bg-action active:bg-action"
              />
            )}
          </>
        )}

        <main className="bg-texture min-w-0 flex-1 overflow-y-auto">
          <Outlet />
        </main>
      </div>

      {/* 常時表示のフッター(コピーライト) */}
      <AppFooter name={branding?.shortName} />
    </div>
  )
}

/** ダークの下部ストリップ(コピーライト+リリースバージョン)。アプリシェル最下段に常時表示 */
function AppFooter({ name }: { name?: string }) {
  return (
    <footer className="flex h-8 shrink-0 items-center gap-4 bg-header px-4 text-[11px] text-header-ink/70">
      <span>{name ?? 'JetUse'} — Copyright © 2026 AISA</span>
      {/* リリースバージョン(feedback 20260620 #13) */}
      <span className="ml-auto tabular-nums opacity-80">v{__APP_VERSION__}</span>
    </footer>
  )
}

/** ページ上部の白いタイトルヘッダ(アイコンチップ + タイトル + アクション) */
export function PageBand({
  icon, title, subtitle, action, helpKey,
}: {
  icon: string; title: string; subtitle?: string; action?: ReactNode; helpKey?: HelpKey
}) {
  return (
    <div className="flex items-center gap-3 border-b border-line bg-surface px-4 py-4 sm:px-6">
      <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-rw bg-band text-xl text-band-ink">
        {isIconName(icon) ? <NavIcon name={icon} className="h-6 w-6" /> : icon}
      </span>
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <h1 className="truncate text-2xl font-bold leading-tight">{title}</h1>
          {/* 機能のヘルプ(構成図ポップアップ — feedback 20260620 #4) */}
          {helpKey && <HelpMark topic={helpKey} />}
        </div>
        {subtitle && <p className="mt-0.5 truncate text-xs text-ink-muted">{subtitle}</p>}
      </div>
      {action && <div className="ml-auto shrink-0">{action}</div>}
    </div>
  )
}

export function PageContainer({
  icon, title, subtitle, action, wide, helpKey, children,
}: {
  icon: string
  title: string
  subtitle?: string
  action?: ReactNode
  wide?: boolean // 出力が広い方が見やすいページ(ユースケース実行/ビルダー)用
  helpKey?: HelpKey // タイトル横にヘルプ(構成図)を出す(feedback 20260620 #4)
  children: ReactNode
}) {
  return (
    <div>
      <PageBand icon={icon} title={title} subtitle={subtitle} action={action} helpKey={helpKey} />
      <div className={`mx-auto px-6 py-6 ${wide ? 'max-w-screen-2xl' : 'max-w-5xl'}`}>
        {children}
      </div>
    </div>
  )
}
