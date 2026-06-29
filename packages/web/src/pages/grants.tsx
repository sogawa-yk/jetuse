/** Platform スコープ承認 UI（BE-05 / PAPI-02 の到達経路）。
 *  管理者(ADMIN_USERS=SA)が、インストール済みプラグインが manifest で宣言した Platform スコープを、
 *  プロジェクト(テナント OCID)単位でレビューして承認/失効する。承認は二重閉包(UI は宣言スコープのみ
 *  チェック可能、サーバが manifest.permissions ∩ PLATFORM_SCOPES に再閉包)。非管理者は API が 403。 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { authHeaders, useUser } from '../auth'
import { PageContainer } from '../components/layout'
import { DataTable, OciButton, Panel, StatusBadge, type Column } from '../components/oci'
import { usePrefs } from '../prefs'

export type Candidate = {
  plugin_id: string
  version: string | null
  name: string | null
  declared_scopes: string[]
}

export type Grant = {
  id: string
  tenant: string
  plugin_id: string
  source_version: string
  scopes: string[]
  status: string
  approved_by: string
  updated_at: string | null
}

export default function Grants() {
  const { t } = usePrefs()
  const user = useUser()
  const [candidates, setCandidates] = useState<Candidate[]>([])
  const [grants, setGrants] = useState<Grant[]>([])
  const [status, setStatus] = useState<'loading' | 'ready' | 'forbidden' | 'error'>('loading')
  const [pluginId, setPluginId] = useState('')
  const [tenant, setTenant] = useState('')
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState<'approve' | string | null>(null)
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  const [reloadKey, setReloadKey] = useState(0)

  const selected = useMemo(
    () => candidates.find((c) => c.plugin_id === pluginId) ?? null,
    [candidates, pluginId],
  )

  // 候補(インストール済み×宣言スコープ)とグラント一覧を取得。403 は forbidden 表示に倒す。
  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const [cRes, gRes] = await Promise.all([
          fetch('/api/platform/grants/candidates', { headers: authHeaders(user) }),
          fetch('/api/platform/grants', { headers: authHeaders(user) }),
        ])
        if (cancelled) return
        if (cRes.status === 403 || gRes.status === 403) {
          setStatus('forbidden')
          return
        }
        if (!cRes.ok || !gRes.ok) {
          setStatus('error')
          return
        }
        const cData = await cRes.json()
        const gData = await gRes.json()
        if (cancelled) return
        setCandidates(cData.candidates ?? [])
        setGrants(gData.grants ?? [])
        setStatus('ready')
      } catch {
        if (!cancelled) setStatus('error')
      }
    })()
    return () => {
      cancelled = true
    }
  }, [user, reloadKey])

  // プラグイン切替時は宣言スコープを全選択(レビューしてから外す運用)。
  const onPickPlugin = useCallback(
    (id: string) => {
      setPluginId(id)
      const c = candidates.find((x) => x.plugin_id === id)
      setPicked(new Set(c?.declared_scopes ?? []))
      setMsg(null)
    },
    [candidates],
  )

  const toggleScope = (scope: string) => {
    setPicked((prev) => {
      const next = new Set(prev)
      if (next.has(scope)) next.delete(scope)
      else next.add(scope)
      return next
    })
  }

  const approve = async () => {
    if (!selected || !tenant.trim() || picked.size === 0) return
    setBusy('approve')
    setMsg(null)
    try {
      const res = await fetch('/api/platform/grants', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
        body: JSON.stringify({
          tenant: tenant.trim(),
          plugin_id: selected.plugin_id,
          version: selected.version ?? undefined,
          scopes: [...picked],
        }),
      })
      if (!res.ok) {
        const d = await res.json().catch(() => ({}))
        setMsg({ kind: 'err', text: d.detail ?? t('grants.actionFailed') })
        return
      }
      setMsg({ kind: 'ok', text: t('grants.approveOk') })
      setReloadKey((k) => k + 1)
    } catch {
      setMsg({ kind: 'err', text: t('grants.actionFailed') })
    } finally {
      setBusy(null)
    }
  }

  const revoke = async (g: Grant) => {
    setBusy(g.id)
    setMsg(null)
    try {
      const res = await fetch('/api/platform/grants', {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
        body: JSON.stringify({ tenant: g.tenant, plugin_id: g.plugin_id }),
      })
      if (!res.ok) {
        const d = await res.json().catch(() => ({}))
        setMsg({ kind: 'err', text: d.detail ?? t('grants.actionFailed') })
        return
      }
      setReloadKey((k) => k + 1)
    } catch {
      setMsg({ kind: 'err', text: t('grants.actionFailed') })
    } finally {
      setBusy(null)
    }
  }

  const cols: Column<Grant>[] = [
    { key: 'tenant', label: t('grants.tenant'), render: (g) => <code className="text-xs">{g.tenant}</code> },
    { key: 'plugin_id', label: t('grants.plugin'), render: (g) => `${g.plugin_id}@${g.source_version}` },
    {
      key: 'scopes',
      label: t('grants.scopes'),
      render: (g) => (
        <div className="flex flex-wrap gap-1">
          {g.scopes.map((s) => (
            <span key={s} className="rounded-full bg-action-soft px-2 py-0.5 text-[11px]">{s}</span>
          ))}
        </div>
      ),
    },
    {
      key: 'status',
      label: t('grants.status'),
      render: (g) => <StatusBadge kind={g.status === 'ACTIVE' ? 'ok' : 'neutral'}>{g.status}</StatusBadge>,
    },
    { key: 'approved_by', label: t('grants.approvedBy') },
    {
      key: 'actions',
      label: t('grants.actions'),
      render: (g) =>
        g.status === 'ACTIVE' ? (
          <OciButton variant="ghost" onClick={() => revoke(g)} disabled={busy === g.id}>
            {busy === g.id ? t('grants.revoking') : t('grants.revoke')}
          </OciButton>
        ) : null,
    },
  ]

  return (
    <PageContainer wide icon="admin" title={t('grants.title')} subtitle={t('grants.lead')}>
      {status === 'forbidden' && (
        <p className="rounded-rw bg-pill-err px-4 py-3 text-sm text-pill-err-ink">{t('grants.forbidden')}</p>
      )}
      {status === 'error' && (
        <p className="rounded-rw bg-pill-err px-4 py-3 text-sm text-pill-err-ink">{t('grants.loadError')}</p>
      )}
      {status === 'loading' && <p className="px-1 py-3 text-sm text-ink-muted">…</p>}

      {status === 'ready' && (
        <div className="space-y-6">
          <Panel title={t('grants.formTitle')}>
            {candidates.length === 0 ? (
              <p className="text-sm text-ink-muted">{t('grants.noCandidates')}</p>
            ) : (
              <div className="space-y-4">
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <label className="block text-sm">
                    <span className="mb-1 block text-ink-muted">{t('grants.plugin')}</span>
                    <select
                      value={pluginId}
                      onChange={(e) => onPickPlugin(e.target.value)}
                      className="w-full rounded-rw border border-line bg-surface px-3 py-2"
                    >
                      <option value="">{t('grants.selectPlugin')}</option>
                      {candidates.map((c) => (
                        <option key={c.plugin_id} value={c.plugin_id}>
                          {c.name ? `${c.name} (${c.plugin_id})` : c.plugin_id}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="block text-sm">
                    <span className="mb-1 block text-ink-muted">{t('grants.tenant')}</span>
                    <input
                      value={tenant}
                      onChange={(e) => setTenant(e.target.value)}
                      placeholder="ocid1.tenancy.oc1..."
                      className="w-full rounded-rw border border-line bg-surface px-3 py-2 font-mono text-xs"
                    />
                  </label>
                </div>

                {selected && (
                  <fieldset className="rounded-rw border border-line p-3">
                    <legend className="px-1 text-xs text-ink-muted">{t('grants.scopes')}</legend>
                    <div className="flex flex-wrap gap-3">
                      {selected.declared_scopes.map((s) => (
                        <label key={s} className="flex items-center gap-1.5 text-sm">
                          <input type="checkbox" checked={picked.has(s)} onChange={() => toggleScope(s)} />
                          <code className="text-xs">{s}</code>
                        </label>
                      ))}
                    </div>
                  </fieldset>
                )}

                {msg && (
                  <p
                    className={`rounded-rw px-3 py-2 text-sm ${msg.kind === 'ok' ? 'bg-pill-ok text-pill-ok-ink' : 'bg-pill-err text-pill-err-ink'}`}
                  >
                    {msg.text}
                  </p>
                )}

                <OciButton
                  onClick={approve}
                  disabled={!selected || !tenant.trim() || picked.size === 0 || busy === 'approve'}
                >
                  {busy === 'approve' ? t('grants.approving') : t('grants.approve')}
                </OciButton>
              </div>
            )}
          </Panel>

          <Panel title={t('grants.existing')}>
            {grants.length === 0 ? (
              <p className="text-sm text-ink-muted">{t('grants.none')}</p>
            ) : (
              <DataTable columns={cols} rows={grants} rowKey={(g) => g.id} />
            )}
          </Panel>
        </div>
      )}
    </PageContainer>
  )
}
