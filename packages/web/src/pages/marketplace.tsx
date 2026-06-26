/* eslint-disable react-refresh/only-export-components -- 純粋ヘルパー(filter/tags/version比較)を単体テスト用に同居 */
import { useEffect, useMemo, useState } from 'react'
import { authHeaders, useUser } from '../auth'
import { NavIcon, isIconName } from '../components/icons'
import { PageContainer } from '../components/layout'
import { usePrefs } from '../prefs'

/** マーケットプレイス(PLG-06): レジストリ配布一覧の閲覧・検索・タグ絞り込み・詳細表示と
 *  install/uninstall(PLG-03 を /api/marketplace 経由で呼ぶ)。インストール済み・更新あり(版比較)を表示。 */

export type Plugin = {
  id: string
  version: string
  kind?: string | null
  name: string
  description?: string
  publisher?: string | null
  tags?: string[]
  icon?: string | null
  versions?: string[]
  installed?: boolean
  installed_versions?: string[]
  uninstallable_versions?: string[]
  update_available?: boolean
  installable?: boolean
  can_uninstall?: boolean
}

export type PluginDetail = Plugin & {
  permissions?: string[]
  requires?: Record<string, unknown>
  license?: string | null
  signed?: boolean
}

// --- 純粋ヘルパ(単体テスト対象) ---

/** q(id/name/description 部分一致, 大小無視)・tag でカタログを絞り込む。 */
export function filterPlugins(plugins: Plugin[], q: string, tag: string): Plugin[] {
  const needle = q.trim().toLowerCase()
  return plugins.filter((p) => {
    if (tag && !(p.tags ?? []).includes(tag)) return false
    if (!needle) return true
    const hay = `${p.id} ${p.name} ${p.description ?? ''}`.toLowerCase()
    return hay.includes(needle)
  })
}

/** カタログ全体のタグ集合(重複なし・ソート済み)。 */
export function allTags(plugins: Plugin[]): string[] {
  return [...new Set(plugins.flatMap((p) => p.tags ?? []))].sort()
}

/** 版比較ラベル: インストール版 → 最新版(更新あり時のみ)。 */
export function updateLabel(p: Plugin): string | null {
  if (!p.update_available) return null
  const from = (p.installed_versions ?? [])[0]
  if (!from) return null
  return `v${from} → v${p.version}`
}

// --- ページ ---

export default function Marketplace() {
  const { t } = usePrefs()
  const user = useUser()
  const [plugins, setPlugins] = useState<Plugin[]>([])
  const [query, setQuery] = useState('')
  const [tag, setTag] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const [detail, setDetail] = useState<PluginDetail | null>(null)
  const [status, setStatus] = useState<'loading' | 'ready' | 'unconfigured' | 'error'>(
    'loading',
  )
  const [busy, setBusy] = useState<'install' | 'uninstall' | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  // install/uninstall 後の再取得トリガ(effect 内の同期 setState を避けるためカウンタで回す)。
  const [reloadKey, setReloadKey] = useState(0)

  // カタログ取得。状態更新はすべて fetch の .then/.catch コールバック内で行う
  // (effect 本体での同期 setState を避ける = home.tsx と同じ流儀)。
  useEffect(() => {
    let cancelled = false
    fetch('/api/marketplace/plugins', { headers: authHeaders(user) })
      .then(async (r) => {
        if (cancelled) return
        if (r.status === 503) {
          setStatus('unconfigured')
          return
        }
        if (!r.ok) {
          setStatus('error')
          return
        }
        const data = await r.json()
        if (cancelled) return
        setPlugins(data.plugins ?? [])
        setStatus('ready')
      })
      .catch(() => {
        if (!cancelled) setStatus('error')
      })
    return () => {
      cancelled = true
    }
  }, [user, reloadKey])

  // 詳細の取得(選択プラグインの最新 manifest 全文)。未選択時は何もしない
  // (表示側で detail.id と selected を突き合わせて古い詳細を出さない)。
  useEffect(() => {
    if (!selected) return
    let cancelled = false
    fetch(`/api/marketplace/plugins/${selected}`, { headers: authHeaders(user) })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (!cancelled) setDetail(d)
      })
      .catch(() => {
        if (!cancelled) setDetail(null)
      })
    return () => {
      cancelled = true
    }
  }, [selected, user, plugins])

  const tags = useMemo(() => allTags(plugins), [plugins])
  const visible = useMemo(
    () => filterPlugins(plugins, query, tag),
    [plugins, query, tag],
  )

  const act = async (kind: 'install' | 'uninstall', p: Plugin) => {
    setBusy(kind)
    setActionError(null)
    try {
      const body =
        kind === 'install'
          ? { plugin_id: p.id }
          : {
              plugin_id: p.id,
              // viewer が取込者である版だけを送る(他人の版を送って 404 になるのを防ぐ)。
              version: (p.uninstallable_versions ?? p.installed_versions ?? [p.version])[0],
            }
      const res = await fetch(`/api/marketplace/${kind}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const d = await res.json().catch(() => ({}))
        setActionError(d.detail ?? t('market.actionFailed'))
        return
      }
      setReloadKey((k) => k + 1)
    } catch {
      setActionError(t('market.actionFailed'))
    } finally {
      setBusy(null)
    }
  }

  return (
    <PageContainer
      wide
      icon="market"
      title={t('market.title')}
      subtitle={t('market.lead')}
    >
      {status === 'unconfigured' && (
        <p className="rounded-rw border border-line bg-surface px-4 py-3 text-sm text-ink-muted">
          {t('market.unconfigured')}
        </p>
      )}
      {status === 'error' && (
        <p className="rounded-rw bg-pill-err px-4 py-3 text-sm text-pill-err-ink">
          {t('market.error')}
        </p>
      )}
      {status === 'loading' && (
        <p className="px-1 py-3 text-sm text-ink-muted">{t('market.loading')}</p>
      )}

      {status === 'ready' && (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_360px]">
          {/* 左: 検索 + タグ + 一覧 */}
          <div>
            <div className="mb-4 flex items-center gap-2 rounded-full border border-line bg-surface px-4 py-2 text-sm focus-within:border-action">
              <span aria-hidden className="text-ink-muted">⌕</span>
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={t('market.search')}
                aria-label={t('market.search')}
                className="w-full bg-transparent placeholder:text-ink-muted focus:outline-none"
              />
              {query && (
                <button
                  onClick={() => setQuery('')}
                  aria-label={t('market.clearSearch')}
                  className="shrink-0 text-ink-muted hover:text-ink"
                >
                  ✕
                </button>
              )}
            </div>

            {tags.length > 0 && (
              <div className="mb-4 flex flex-wrap items-center gap-1.5 text-xs">
                <button
                  onClick={() => setTag('')}
                  className={`rounded-full px-2.5 py-1 ${!tag ? 'bg-action-soft font-medium' : 'border border-line hover:border-action'}`}
                >
                  {t('market.allTags')}
                </button>
                {tags.map((tg) => (
                  <button
                    key={tg}
                    onClick={() => setTag(tg === tag ? '' : tg)}
                    className={`rounded-full px-2.5 py-1 ${tg === tag ? 'bg-action-soft font-medium' : 'border border-line hover:border-action'}`}
                  >
                    {tg}
                  </button>
                ))}
              </div>
            )}

            {visible.length === 0 ? (
              <p className="px-1 py-6 text-sm text-ink-muted">{t('market.empty')}</p>
            ) : (
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                {visible.map((p) => (
                  <PluginCard
                    key={p.id}
                    p={p}
                    selected={p.id === selected}
                    onSelect={() => setSelected(p.id)}
                    installedLabel={t('market.installed')}
                    updateLabelText={t('market.updateAvailable')}
                  />
                ))}
              </div>
            )}
          </div>

          {/* 右: 詳細 */}
          <DetailPanel
            t={t}
            plugin={visible.find((p) => p.id === selected) ?? null}
            detail={detail && detail.id === selected ? detail : null}
            busy={busy}
            actionError={actionError}
            onInstall={(p) => act('install', p)}
            onUninstall={(p) => act('uninstall', p)}
          />
        </div>
      )}
    </PageContainer>
  )
}

function PluginCard({
  p, selected, onSelect, installedLabel, updateLabelText,
}: {
  p: Plugin
  selected: boolean
  onSelect: () => void
  installedLabel: string
  updateLabelText: string
}) {
  return (
    <button
      onClick={onSelect}
      aria-pressed={selected}
      className={`group block rounded-rw-xl bg-surface p-4 text-left shadow-rw transition-shadow hover:shadow-rw-md ${
        selected ? 'ring-2 ring-action' : ''
      }`}
    >
      <div className="flex items-center gap-3">
        <span
          aria-hidden
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-rw bg-band/10 text-lg text-band"
        >
          {isIconName(p.icon ?? '') ? (
            <NavIcon name={(p.icon ?? 'market') as Parameters<typeof NavIcon>[0]['name']} className="h-5 w-5" />
          ) : (
            (p.icon ?? '🧩')
          )}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-bold group-hover:text-action">
            {p.name}
          </span>
          <span className="block truncate text-[11px] text-ink-muted">
            {p.publisher} · v{p.version}
          </span>
        </span>
      </div>
      {p.description && (
        <p className="mt-2 line-clamp-2 text-xs leading-relaxed text-ink-muted">
          {p.description}
        </p>
      )}
      <div className="mt-2 flex flex-wrap gap-1.5">
        {p.installed && (
          <span className="rounded-full bg-action-soft px-2 py-0.5 text-[10px] text-ink">
            ✓ {installedLabel}
          </span>
        )}
        {p.update_available && (
          <span className="rounded-full border border-action px-2 py-0.5 text-[10px] text-action">
            ↑ {updateLabelText}
          </span>
        )}
      </div>
    </button>
  )
}

function DetailPanel({
  t, plugin, detail, busy, actionError, onInstall, onUninstall,
}: {
  t: ReturnType<typeof usePrefs>['t']
  plugin: Plugin | null
  detail: PluginDetail | null
  busy: 'install' | 'uninstall' | null
  actionError: string | null
  onInstall: (p: Plugin) => void
  onUninstall: (p: Plugin) => void
}) {
  if (!plugin) {
    return (
      <aside className="rounded-rw-xl border border-dashed border-line bg-surface/60 p-6 text-sm text-ink-muted lg:sticky lg:top-4 lg:self-start">
        {t('market.detail.none')}
      </aside>
    )
  }
  const upd = updateLabel(plugin)
  return (
    <aside className="rounded-rw-xl bg-surface p-5 shadow-rw lg:sticky lg:top-4 lg:self-start">
      <h2 className="text-lg font-bold">{plugin.name}</h2>
      <p className="mt-0.5 text-xs text-ink-muted">
        {plugin.publisher} · v{plugin.version}
        {plugin.kind ? ` · ${plugin.kind}` : ''}
      </p>

      <div className="mt-2 flex flex-wrap gap-1.5">
        {plugin.installed && (
          <span className="rounded-full bg-action-soft px-2 py-0.5 text-[10px] text-ink">
            ✓ {t('market.installed')}
            {(plugin.installed_versions ?? [])[0]
              ? ` v${(plugin.installed_versions ?? [])[0]}`
              : ''}
          </span>
        )}
        {upd && (
          <span className="rounded-full border border-action px-2 py-0.5 text-[10px] text-action">
            ↑ {upd}
          </span>
        )}
        {detail?.signed && (
          <span className="rounded-full border border-line px-2 py-0.5 text-[10px] text-ink-muted">
            {t('market.signed')}
          </span>
        )}
      </div>

      {plugin.description && (
        <p className="mt-3 text-sm leading-relaxed text-ink">{plugin.description}</p>
      )}

      {detail && (detail.permissions ?? []).length > 0 && (
        <div className="mt-4">
          <h3 className="text-xs font-semibold text-ink-muted">{t('market.permissions')}</h3>
          <ul className="mt-1 space-y-0.5">
            {(detail.permissions ?? []).map((perm) => (
              <li key={perm} className="text-xs text-ink">• {perm}</li>
            ))}
          </ul>
        </div>
      )}

      {(plugin.versions ?? []).length > 0 && (
        <div className="mt-4">
          <h3 className="text-xs font-semibold text-ink-muted">{t('market.versions')}</h3>
          <p className="mt-1 text-xs text-ink">{(plugin.versions ?? []).join(', ')}</p>
        </div>
      )}

      {actionError && (
        <p className="mt-4 rounded-rw bg-pill-err px-3 py-2 text-xs text-pill-err-ink">
          {actionError}
        </p>
      )}

      {/* installer が取込可能な kind(usecase/agent)のみ install を出す。未対応 kind は明示。 */}
      {plugin.installable === false && (
        <p className="mt-4 rounded-rw border border-line px-3 py-2 text-xs text-ink-muted">
          {t('market.unsupported')}
        </p>
      )}

      <div className="mt-5 flex gap-2">
        {/* uninstall は取込んだ本人(can_uninstall)にだけ出す(他人の取込定義を消させない)。 */}
        {plugin.installed && plugin.can_uninstall ? (
          <button
            onClick={() => onUninstall(plugin)}
            disabled={busy !== null}
            className="rounded-rw border border-line px-4 py-2 text-sm text-ink-muted hover:bg-pill-err hover:text-pill-err-ink disabled:opacity-50"
          >
            {busy === 'uninstall' ? t('market.uninstalling') : t('market.uninstall')}
          </button>
        ) : null}
        {plugin.installable !== false && (!plugin.installed || plugin.update_available) && (
          <button
            onClick={() => onInstall(plugin)}
            disabled={busy !== null}
            className="rounded-rw bg-cta px-4 py-2 text-sm font-medium text-cta-ink hover:bg-cta-strong disabled:opacity-50"
          >
            {busy === 'install'
              ? t('market.installing')
              : plugin.update_available
                ? t('market.update')
                : t('market.install')}
          </button>
        )}
      </div>
    </aside>
  )
}
