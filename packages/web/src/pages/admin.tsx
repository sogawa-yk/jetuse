/** 管理ダッシュボード(OPS-01): 利用状況(機能/モデル/ユーザー別トークン・概算コスト)。
 *  集計データは /api/admin/usage(ADMIN_USERS制)。非管理者は403案内 */
import { useEffect, useState } from 'react'
import { authHeaders, useUser } from '../auth'
import { PageContainer } from '../components/layout'
import { DataTable, Panel, type Column } from '../components/oci'
import { usePrefs } from '../prefs'

type Usage = {
  days: number
  by_feature: { feature: string; calls: number; input_tokens: number; output_tokens: number }[]
  by_model: { model: string; calls: number; input_tokens: number; output_tokens: number }[]
  by_user: { user: string; calls: number; total_tokens: number }[]
  by_day: { day: string; calls: number; total_tokens: number }[]
}

// 概算コスト(プリセールス用のざっくり指標。実価格はモデル・契約で変動)
// gpt-oss/llamaは安価、geminiは高め。1Mトークンあたりの概算USD(入出力合算の粗い平均)
const COST_PER_MTOK: Record<string, number> = {
  'gpt-oss-120b': 0.3,
  'llama-3.3-70b': 0.2,
  'gemini-2.5-pro': 3.0,
  'gemini-2.5-flash': 0.4,
  'llama-3.2-90b-vision': 0.4,
}

const fmt = (n: number) => n.toLocaleString('en-US')

export default function Admin() {
  const { t } = usePrefs()
  const user = useUser()
  const [days, setDays] = useState(30)
  const [data, setData] = useState<Usage | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let active = true
    void (async () => {
      setLoading(true)
      setError(null)
      try {
        const r = await fetch(`/api/admin/usage?days=${days}`, { headers: authHeaders(user) })
        if (r.status === 403) throw new Error(t('admin.forbidden'))
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        const json = await r.json()
        if (active) setData(json)
      } catch (e) {
        if (active) setError(String(e instanceof Error ? e.message : e))
      } finally {
        if (active) setLoading(false)
      }
    })()
    return () => {
      active = false
    }
  }, [user, days, t])

  const totalTokens = (data?.by_model ?? []).reduce(
    (s, m) => s + m.input_tokens + m.output_tokens,
    0,
  )
  const totalCalls = (data?.by_feature ?? []).reduce((s, f) => s + f.calls, 0)
  const estCost = (data?.by_model ?? []).reduce((s, m) => {
    const rate = COST_PER_MTOK[m.model] ?? 0.3
    return s + ((m.input_tokens + m.output_tokens) / 1_000_000) * rate
  }, 0)

  const maxDayTok = Math.max(1, ...(data?.by_day ?? []).map((d) => d.total_tokens))

  const featureCols: Column<Usage['by_feature'][number]>[] = [
    { key: 'feature', label: t('admin.feature') },
    { key: 'calls', label: t('admin.calls'), render: (r) => fmt(r.calls) },
    { key: 'input_tokens', label: 'in', render: (r) => fmt(r.input_tokens) },
    { key: 'output_tokens', label: 'out', render: (r) => fmt(r.output_tokens) },
  ]
  const modelCols: Column<Usage['by_model'][number]>[] = [
    { key: 'model', label: t('admin.model') },
    { key: 'calls', label: t('admin.calls'), render: (r) => fmt(r.calls) },
    {
      key: 'tok',
      label: t('admin.tokens'),
      render: (r) => fmt(r.input_tokens + r.output_tokens),
    },
    {
      key: 'cost',
      label: t('admin.estCost'),
      render: (r) =>
        '$' +
        (((r.input_tokens + r.output_tokens) / 1_000_000) * (COST_PER_MTOK[r.model] ?? 0.3)).toFixed(
          3,
        ),
    },
  ]
  const userCols: Column<Usage['by_user'][number]>[] = [
    { key: 'user', label: t('admin.user') },
    { key: 'calls', label: t('admin.calls'), render: (r) => fmt(r.calls) },
    { key: 'total_tokens', label: t('admin.tokens'), render: (r) => fmt(r.total_tokens) },
  ]

  return (
    <PageContainer icon="admin" title={t('nav.admin')} subtitle={t('admin.lead')} wide>
      <div className="mb-4 flex items-center gap-2 text-sm">
        <label className="text-ink-muted">{t('admin.period')}</label>
        <select
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
          className="rounded-rw border border-line bg-surface px-2 py-1"
        >
          <option value={7}>7{t('admin.daysUnit')}</option>
          <option value={30}>30{t('admin.daysUnit')}</option>
          <option value={90}>90{t('admin.daysUnit')}</option>
        </select>
      </div>

      {error && (
        <div className="rounded-rw bg-pill-err px-3 py-2 text-sm text-pill-err-ink">{error}</div>
      )}
      {loading && !error && <p className="text-sm text-ink-muted">{t('common.comingSoon')}…</p>}

      {data && !error && (
        <div className="space-y-4">
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
            <Panel>
              <div className="text-xs text-ink-muted">{t('admin.totalCalls')}</div>
              <div className="text-2xl font-bold">{fmt(totalCalls)}</div>
            </Panel>
            <Panel>
              <div className="text-xs text-ink-muted">{t('admin.totalTokens')}</div>
              <div className="text-2xl font-bold">{fmt(totalTokens)}</div>
            </Panel>
            <Panel>
              <div className="text-xs text-ink-muted">{t('admin.estCostTotal')}</div>
              <div className="text-2xl font-bold">${estCost.toFixed(2)}</div>
            </Panel>
          </div>

          <Panel title={t('admin.byDay')}>
            {data.by_day.length === 0 ? (
              <p className="text-xs text-ink-muted">{t('admin.noData')}</p>
            ) : (
              <div className="flex items-end gap-1" style={{ height: '120px' }}>
                {data.by_day.map((d) => (
                  <div key={d.day} className="flex flex-1 flex-col items-center justify-end" title={`${d.day}: ${fmt(d.total_tokens)} tok / ${d.calls} calls`}>
                    <div
                      className="w-full rounded-t bg-action"
                      style={{ height: `${Math.max(2, (d.total_tokens / maxDayTok) * 100)}%` }}
                    />
                    <span className="mt-1 text-[9px] text-ink-muted">{d.day.slice(5)}</span>
                  </div>
                ))}
              </div>
            )}
          </Panel>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <Panel title={t('admin.byFeature')}>
              <DataTable columns={featureCols} rows={data.by_feature} rowKey={(r) => r.feature} />
            </Panel>
            <Panel title={t('admin.byModel')}>
              <DataTable columns={modelCols} rows={data.by_model} rowKey={(r) => r.model} />
            </Panel>
          </div>
          <Panel title={t('admin.byUser')}>
            <DataTable columns={userCols} rows={data.by_user} rowKey={(r) => r.user} />
          </Panel>
          <p className="text-[11px] text-ink-muted">{t('admin.costNote')}</p>
        </div>
      )}
    </PageContainer>
  )
}
