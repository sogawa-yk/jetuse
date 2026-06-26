/** コア同梱 sample-app SBA-B「在庫・受発注照会」(SBA-03 / NL2SQL)。
 *
 *  業務データ(在庫 / 受発注)を JetUse のデザイン部品(DataTable / Panel / OciButton)で一覧し、
 *  「AI照会」コンソールに NL2SQL を埋め込む:
 *    - 日本語の質問 → 生成SQL(nl2sql スロット) → 確認・編集 → 読取専用実行(/api/dbchat/execute)
 *      → 結果テーブル → グラフ化(chart スロット, 既存 ResultChart で描画)
 *  生成・グラフ化はいずれも sample-app の slot invoke API(POST /api/sample-apps/:id/slots/:key/invoke)を
 *  流用し、実行は SELECT 限定・行数上限・タイムアウトの読取専用ガード(SQL-02)をそのまま通す。
 *  SBA-A と同じ「業務アプリのデータに AI を組み込む」型のリファレンス実装。 */
import { useMemo, useState } from 'react'
import { authHeaders, reauthenticate, useUser } from '../auth'
import { DataTable, OciButton, Panel, type Column } from '../components/oci'
import { ResultChart, type ChartSpec } from '../components/resultchart'
import { usePrefs } from '../prefs'

type Field = { name: string; type: string; label?: string | null; required?: boolean }
type Dataset = { name: string; label?: string | null; fields: Field[]; seed: Record<string, unknown>[] }
type AiSlot = { key: string; title: string; capability: string }
export type SbaBApp = {
  id: string
  name: string
  description?: string
  icon?: string
  slot_bindings: Record<string, boolean>
  definition: { screens: unknown[]; datasets: Dataset[]; aiSlots: AiSlot[]; summary?: string }
}

type Result = { columns: string[]; rows: string[][]; row_count: number; truncated: boolean }

type TKey = Parameters<ReturnType<typeof usePrefs>['t']>[0]
type T = (k: TKey) => string

const SAMPLE_QUESTIONS = [
  '倉庫別の在庫数を集計して',
  '在庫数が発注点を下回っている商品は？',
  'カテゴリ別の在庫金額（在庫数×単価）の合計は？',
  '月別の受注金額の推移を見せて',
  '取引先別の受注金額トップ5は？',
]

function str(v: unknown): string {
  return typeof v === 'string' ? v : v == null ? '' : String(v)
}

export function Nl2SqlApp({ app }: { app: SbaBApp }) {
  const { t } = usePrefs()
  const user = useUser()
  const [view, setView] = useState<'query' | 'inventory' | 'orders'>('query')

  const datasets = useMemo(() => app.definition?.datasets ?? [], [app])
  const inventory = useMemo(() => datasets.find((d) => d.name === 'inventory'), [datasets])
  const orders = useMemo(() => datasets.find((d) => d.name === 'orders'), [datasets])

  // capability → 束縛済み slot key。
  const slotKeyByCap = useMemo(() => {
    const map: Record<string, string> = {}
    for (const s of app.definition?.aiSlots ?? []) {
      if (app.slot_bindings?.[s.key]) map[s.capability] = s.key
    }
    return map
  }, [app])

  const callSlot = async (cap: string, body: Record<string, unknown>): Promise<Record<string, unknown>> => {
    const slotKey = slotKeyByCap[cap]
    if (!slotKey) throw new Error('slot unavailable')
    const res = await fetch(`/api/sample-apps/${app.id}/slots/${slotKey}/invoke`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
      body: JSON.stringify(body),
    })
    if (res.status === 401) {
      reauthenticate()
      throw new Error(t('uc.sessionLost'))
    }
    if (!res.ok) {
      const b = (await res.json().catch(() => null)) as { detail?: string } | null
      throw new Error(b?.detail || `HTTP ${res.status}`)
    }
    return (await res.json()) as Record<string, unknown>
  }

  const tabBtn = (key: 'query' | 'inventory' | 'orders', label: string) => (
    <button
      type="button"
      onClick={() => setView(key)}
      aria-pressed={view === key}
      className={`rounded-rw px-3 py-1.5 text-sm ${
        view === key
          ? 'bg-action-soft font-medium text-ink'
          : 'border border-line text-ink-muted hover:border-action hover:text-action'
      }`}
    >
      {label}
    </button>
  )

  return (
    <div className="space-y-4">
      <div className="flex gap-1.5">
        {tabBtn('query', t('sba_b.tab.query'))}
        {tabBtn('inventory', t('sba_b.tab.inventory'))}
        {tabBtn('orders', t('sba_b.tab.orders'))}
      </div>

      {view === 'query' ? (
        <QueryConsole
          appId={app.id}
          datasets={datasets}
          callSlot={callSlot}
          hasChart={!!slotKeyByCap.chart}
          t={t}
        />
      ) : view === 'inventory' ? (
        <DatasetTable dataset={inventory} t={t} />
      ) : (
        <DatasetTable dataset={orders} t={t} />
      )}
    </div>
  )
}

/* ---------------- 業務データ一覧(非AI) ---------------- */

function DatasetTable({ dataset, t }: { dataset: Dataset | undefined; t: T }) {
  if (!dataset) return <p className="text-sm text-ink-muted">{t('sba_b.empty')}</p>
  const columns: Column<Record<string, unknown>>[] = dataset.fields.map((f) => ({
    key: f.name,
    label: f.label || f.name,
    render: (r) => str(r[f.name]) || <span className="text-ink-muted/60">—</span>,
  }))
  return (
    <Panel title={`📦 ${dataset.label || dataset.name}`}>
      {dataset.seed.length === 0 ? (
        <p className="py-6 text-center text-sm text-ink-muted">{t('sba_b.empty')}</p>
      ) : (
        <DataTable
          columns={columns}
          rows={dataset.seed}
          rowKey={(r) => str(r.product_code) || str(r.order_id) || str(r.id) || JSON.stringify(r)}
        />
      )}
    </Panel>
  )
}

/* ---------------- AI照会コンソール(NL2SQL + Chart) ---------------- */

function QueryConsole({
  appId,
  datasets,
  callSlot,
  hasChart,
  t,
}: {
  appId: string
  datasets: Dataset[]
  callSlot: (cap: string, body: Record<string, unknown>) => Promise<Record<string, unknown>>
  hasChart: boolean
  t: T
}) {
  const [question, setQuestion] = useState('')
  const [sql, setSql] = useState('')
  const [result, setResult] = useState<Result | null>(null)
  const [chart, setChart] = useState<ChartSpec | null>(null)
  const [busy, setBusy] = useState<'' | 'generate' | 'execute' | 'chart'>('')
  const [error, setError] = useState<string | null>(null)
  const user = useUser()

  const run = async (name: 'generate' | 'execute' | 'chart', fn: () => Promise<void>) => {
    setBusy(name)
    setError(null)
    try {
      await fn()
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e))
    } finally {
      setBusy('')
    }
  }

  const generate = () =>
    run('generate', async () => {
      setSql('')
      setResult(null)
      setChart(null)
      const res = await callSlot('nl2sql', { input: question })
      setSql(str(res.sql))
    })

  const execute = () =>
    run('execute', async () => {
      setResult(null)
      setChart(null)
      // sample-app 専用 execute(SQL-02 ガード + SBA-B テーブル許可リストを実行段でも強制 / B1)。
      const res = await fetch(`/api/sample-apps/${appId}/dbchat/execute`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
        body: JSON.stringify({ sql }),
      })
      if (res.status === 401) {
        reauthenticate()
        throw new Error(t('uc.sessionLost'))
      }
      const data = await res.json()
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`)
      setResult(data as Result)
    })

  const suggestChart = () =>
    run('chart', async () => {
      if (!result) return
      const spec = await callSlot('chart', {
        input: question,
        columns: result.columns,
        rows: result.rows.slice(0, 50),
      })
      setChart(spec as ChartSpec)
    })

  return (
    <div className="space-y-4">
      {/* 照会できるデータ(スキーマ) */}
      <div className="rounded-rw border border-line bg-surface p-4">
        <p className="mb-2 text-sm font-semibold text-ink-muted">📋 {t('sba_b.schema.title')}</p>
        <p className="mb-2 text-xs text-ink-muted">{t('sba_b.schema.lead')}</p>
        <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
          {datasets.map((d) => (
            <div key={d.name} className="rounded-rw border border-line bg-bg px-2.5 py-1.5">
              <span className="font-mono text-xs font-semibold">{d.name.toUpperCase()}</span>
              <span className="ml-2 text-[11px] text-ink-muted">{d.label}</span>
              <div className="mt-0.5 text-[11px] leading-snug text-ink-muted">
                {d.fields.map((f) => f.name).join(', ')}
              </div>
            </div>
          ))}
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          <span className="text-xs text-ink-muted">{t('db.samples')}:</span>
          {SAMPLE_QUESTIONS.map((q) => (
            <button
              key={q}
              type="button"
              onClick={() => setQuestion(q)}
              className="rounded-full border border-line px-2.5 py-1 text-xs hover:border-action hover:text-action"
            >
              {q}
            </button>
          ))}
        </div>
      </div>

      {/* 質問 */}
      <div className="rounded-rw border border-line bg-surface p-4">
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            if (question.trim() && busy === '') void generate()
          }}
        >
          <textarea
            rows={2}
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder={t('sba_b.placeholder')}
            aria-label={t('sba_b.placeholder')}
            className="min-w-0 flex-1 resize-y rounded-rw border border-line bg-bg px-3 py-2 text-sm outline-none focus:border-action"
          />
          <button
            type="submit"
            disabled={!question.trim() || busy !== ''}
            className="rounded-rw bg-cta px-4 py-2 text-sm font-medium text-cta-ink hover:bg-cta-strong disabled:opacity-40"
          >
            {busy === 'generate' ? t('db.generating') : t('db.generate')}
          </button>
        </form>
        {busy === 'generate' && (
          <p className="mt-2 text-xs text-ink-muted">⏳ {t('db.generating')} ({t('sba_b.generating.note')})</p>
        )}
      </div>

      {/* 生成SQL(確認・編集) */}
      {sql && (
        <div className="rounded-rw border border-line bg-surface p-4">
          <div className="mb-2 flex items-center justify-between">
            <h2 className="text-sm font-semibold text-ink-muted">{t('db.sql')}</h2>
            <span className="flex gap-2">
              <button
                type="button"
                onClick={() => void navigator.clipboard?.writeText(sql)}
                className="text-xs text-ink-muted hover:text-action"
              >
                ⧉ {t('chat.copy')}
              </button>
              <OciButton onClick={() => void execute()} disabled={busy !== '' || !sql.trim()}>
                {busy === 'execute' ? t('db.executing') : `▶ ${t('db.execute')}`}
              </OciButton>
            </span>
          </div>
          <textarea
            rows={Math.min(14, Math.max(4, sql.split('\n').length + 1))}
            value={sql}
            onChange={(e) => setSql(e.target.value)}
            spellCheck={false}
            aria-label={t('db.sql')}
            className="w-full resize-y rounded-rw border border-line bg-bg p-3 font-mono text-xs leading-relaxed outline-none focus:border-action"
          />
          <p className="mt-1 text-[11px] text-ink-muted">{t('db.guard')}</p>
        </div>
      )}

      {error && (
        <div className="rounded-rw border border-primary bg-primary-soft px-3 py-2 text-sm">⚠ {error}</div>
      )}

      {/* 結果テーブル + グラフ */}
      {result && (
        <div className="rounded-rw border border-line bg-surface p-4">
          <div className="mb-2 flex items-center justify-between">
            <h2 className="text-sm font-semibold text-ink-muted">
              {t('db.result')}: {result.row_count}
              {t('db.rows')}
              {result.truncated && <span className="ml-2 text-primary-strong">{t('db.truncated')}</span>}
            </h2>
            {hasChart && result.row_count > 0 && (
              <button
                type="button"
                onClick={() => void suggestChart()}
                disabled={busy !== ''}
                className="rounded-rw border border-line px-3 py-1 text-xs hover:border-action hover:text-action disabled:opacity-40"
              >
                {busy === 'chart' ? t('db.charting') : `📊 ${t('db.chart')}`}
              </button>
            )}
          </div>
          {chart && chart.type !== 'none' && (
            <div className="mb-3 rounded-rw border border-line bg-bg p-3">
              <ResultChart spec={chart} columns={result.columns} rows={result.rows} />
              <div className="mt-2 flex items-center gap-2 text-[11px] text-ink-muted">
                {chart.reason && <span>💡 {chart.reason}</span>}
                <select
                  value={chart.type}
                  onChange={(e) => setChart({ ...chart, type: e.target.value as ChartSpec['type'] })}
                  className="ml-auto rounded-rw border border-line bg-surface px-1.5 py-0.5 outline-none"
                  aria-label="chart type"
                >
                  <option value="bar">bar</option>
                  <option value="line">line</option>
                  <option value="pie">pie</option>
                </select>
              </div>
            </div>
          )}
          {chart && chart.type === 'none' && (
            <p className="mb-3 text-xs text-ink-muted">ⓘ {t('db.chart.none')}: {chart.reason}</p>
          )}
          <div className="max-h-[55vh] overflow-auto">
            <table className="min-w-full border-collapse text-xs">
              <thead className="sticky top-0 bg-bg">
                <tr>
                  {result.columns.map((c) => (
                    <th key={c} className="border border-line px-2 py-1.5 text-left font-semibold">
                      {c}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {result.rows.map((r, i) => (
                  <tr key={i} className="odd:bg-bg/40">
                    {r.map((c, j) => (
                      <td key={j} className="border border-line px-2 py-1">
                        {c}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
