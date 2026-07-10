/** デモ一覧(2026-07-09 施主指示): デモビルダーで作成したデモをここから開いて確認できる。
 *  データは GET /api/demos(自分の所有・updated_at DESC)。各デモの実体は
 *  /api/demos/{id}/app/ を新タブで開く(デモビルダーの openPreview と同じ一回性コード方式)。 */
import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { authHeaders, useUser } from '../auth'
import { PageContainer } from '../components/layout'
import { StatusBadge } from '../components/oci'
import { usePrefs } from '../prefs'

type Demo = {
  id: string
  name: string
  description?: string | null
  status: string
  visibility?: string
  created_at?: string
  updated_at?: string
  config?: { frontend?: { generator?: { model?: string } } }
}

const STATUS: Record<string, { kind: 'ok' | 'warn' | 'err'; key: string }> = {
  ready: { kind: 'ok', key: 'demos.st.ready' },
  failed: { kind: 'err', key: 'demos.st.failed' },
}
const pending = { kind: 'warn' as const, key: 'demos.st.pending' }

export default function Demos() {
  const { t, lang } = usePrefs()
  const user = useUser()
  const [demos, setDemos] = useState<Demo[]>([])
  const [loading, setLoading] = useState(true)
  // エラーは翻訳キーで保持し描画時に t() する(load を t 非依存にして再フェッチループを避ける)
  const [errorKey, setErrorKey] = useState<string | null>(null)

  // 同期 setState を effect 本体で呼ばない(loading の初期値 true に依拠。更新ボタンは onClick で立てる)
  const load = useCallback(() => {
    fetch('/api/demos', { headers: authHeaders(user) })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d: { demos: Demo[] }) => {
        setDemos((d.demos ?? []).filter((x) => x.status !== 'deleting'))
        setErrorKey(null)
      })
      .catch(() => setErrorKey('demos.loadError'))
      .finally(() => setLoading(false))
  }, [user])

  useEffect(load, [load])

  // デモ本体を新タブで開く。一回性コード(app-session)を添える。AUTH オフ配備では
  // 発行できず素の /app/ が dev-user で通るためプレーン URL にフォールバックする。
  const openDemo = async (id: string) => {
    const base = `/api/demos/${id}/app/`
    let url = base
    try {
      const r = await fetch(`/api/demos/${id}/app-session`, {
        method: 'POST',
        headers: authHeaders(user),
      })
      if (r.ok) {
        const j = (await r.json()) as { code: string }
        url = `${base}?c=${encodeURIComponent(j.code)}`
      }
    } catch {
      /* AUTH オフ: プレーン URL で開く */
    }
    window.open(url, '_blank', 'noopener')
  }

  const removeDemo = async (id: string, name: string) => {
    if (!window.confirm(t('demos.deleteConfirm').replace('{name}', name))) return
    try {
      const r = await fetch(`/api/demos/${id}`, { method: 'DELETE', headers: authHeaders(user) })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      setDemos((ds) => ds.filter((d) => d.id !== id))
    } catch {
      setErrorKey('demos.deleteError')
    }
  }

  const fmtDate = (s?: string) =>
    s ? new Date(s).toLocaleString(lang === 'ja' ? 'ja-JP' : 'en-US', {
      year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    }) : ''

  return (
    <PageContainer
      icon="gallery"
      title={t('nav.demos')}
      subtitle={t('demos.lead')}
      action={
        <div className="flex items-center gap-2">
          <button
            onClick={() => { setLoading(true); load() }}
            className="rounded-rw border border-line px-3 py-1.5 text-sm text-ink-muted hover:border-action hover:text-action"
          >
            ⟳ {t('demos.refresh')}
          </button>
          <Link
            to="/demo-builder"
            className="rounded-rw bg-cta px-3.5 py-1.5 text-sm font-medium text-cta-ink hover:bg-cta-strong"
          >
            ＋ {t('demos.new')}
          </Link>
        </div>
      }
    >
      {loading ? (
        <p className="text-sm text-ink-muted">{t('demos.loading')}</p>
      ) : errorKey ? (
        <p className="rounded-rw border border-line bg-surface px-4 py-3 text-sm text-pill-err-ink">
          {t(errorKey as Parameters<typeof t>[0])}
        </p>
      ) : demos.length === 0 ? (
        <div className="rounded-rw-xl bg-surface px-6 py-12 text-center shadow-rw">
          <p className="text-sm font-medium">{t('demos.empty')}</p>
          <p className="mt-1 text-xs text-ink-muted">{t('demos.emptyHint')}</p>
          <Link
            to="/demo-builder"
            className="mt-4 inline-block rounded-rw bg-cta px-4 py-2 text-sm font-medium text-cta-ink hover:bg-cta-strong"
          >
            ＋ {t('demos.new')}
          </Link>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {demos.map((d) => {
            const st = STATUS[d.status] ?? pending
            const model = d.config?.frontend?.generator?.model
            const ready = d.status === 'ready'
            return (
              <div key={d.id} className="flex flex-col rounded-rw-xl bg-surface p-4 shadow-rw">
                <div className="flex items-start justify-between gap-2">
                  <h2 className="min-w-0 flex-1 truncate text-sm font-bold" title={d.name}>
                    {d.name || t('demos.untitled')}
                  </h2>
                  <StatusBadge kind={st.kind}>{t(st.key as Parameters<typeof t>[0])}</StatusBadge>
                </div>
                {d.description && (
                  <p className="mt-1 line-clamp-2 text-xs text-ink-muted">{d.description}</p>
                )}
                <dl className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-ink-muted">
                  {model && (
                    <span className="rounded-full bg-band/10 px-2 py-0.5 font-medium text-band">
                      {model}
                    </span>
                  )}
                  <span className="tabular-nums">{fmtDate(d.updated_at || d.created_at)}</span>
                </dl>
                <div className="mt-3 flex items-center gap-2 border-t border-line pt-3">
                  <button
                    onClick={() => openDemo(d.id)}
                    disabled={!ready}
                    className="rounded-rw bg-cta px-3 py-1.5 text-sm font-medium text-cta-ink transition-colors hover:bg-cta-strong disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    {t('demos.open')} ↗
                  </button>
                  <button
                    onClick={() => removeDemo(d.id, d.name || t('demos.untitled'))}
                    className="ml-auto rounded-rw px-2.5 py-1.5 text-sm text-ink-muted hover:bg-pill-err/10 hover:text-pill-err-ink"
                  >
                    {t('demos.delete')}
                  </button>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </PageContainer>
  )
}
