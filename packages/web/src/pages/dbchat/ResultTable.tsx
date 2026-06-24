/** 結果テーブル + グラフ提案(dbchat.tsx分割: review-validation.md §7)。
 *  /api/dbchat/chart でグラフ仕様を提案し、resultchart.tsx(Chart.js動的import)で描画する(SQL-03)。 */
import { useState } from 'react'
import { authHeaders, useUser } from '../../auth'
import { ResultChart, type ChartSpec } from '../../components/resultchart'
import { usePrefs } from '../../prefs'
import type { Result } from './types'

export function ResultTable({
  result,
  question,
  onError,
}: {
  result: Result
  /** グラフ提案に使う元の質問。 */
  question: string
  onError: (msg: string) => void
}) {
  const { t } = usePrefs()
  const user = useUser()
  const [chart, setChart] = useState<ChartSpec | null>(null)
  const [charting, setCharting] = useState(false)

  const suggestChart = async () => {
    if (charting) return
    setCharting(true)
    onError('')
    try {
      const res = await fetch('/api/dbchat/chart', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
        body: JSON.stringify({
          question: question.trim(),
          columns: result.columns,
          rows: result.rows.slice(0, 20),
        }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`)
      setChart(data)
    } catch (e) {
      onError(String(e instanceof Error ? e.message : e))
    } finally {
      setCharting(false)
    }
  }

  return (
    <div className="rounded-rw border border-line bg-surface p-4">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-ink-muted">
          {t('db.result')}: {result.row_count}
          {t('db.rows')}
          {result.truncated && (
            <span className="ml-2 text-primary-strong">{t('db.truncated')}</span>
          )}
        </h2>
        <button
          onClick={() => void suggestChart()}
          disabled={charting}
          className="rounded-rw border border-line px-3 py-1 text-xs hover:border-action hover:text-action disabled:opacity-40"
        >
          {charting ? t('db.charting') : `📊 ${t('db.chart')}`}
        </button>
      </div>
      {chart && chart.type !== 'none' && (
        <div className="mb-3 rounded-rw border border-line bg-bg p-3">
          <ResultChart spec={chart} columns={result.columns} rows={result.rows} />
          <div className="mt-2 flex items-center gap-2 text-[11px] text-ink-muted">
            {chart.reason && <span>💡 {chart.reason}</span>}
            <select
              value={chart.type}
              onChange={(e) =>
                setChart({ ...chart, type: e.target.value as ChartSpec['type'] })
              }
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
        <p className="mb-3 text-xs text-ink-muted">
          ⓘ {t('db.chart.none')}: {chart.reason}
        </p>
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
  )
}
