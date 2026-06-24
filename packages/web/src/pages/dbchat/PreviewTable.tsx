/** テーブル中身プレビュー(先頭N行)の小テーブル(dbchat.tsx分割: review-validation.md §7)。 */
import type { Result } from './types'

export function PreviewTable({ data }: { data: Result }) {
  return (
    <div className="mt-1.5 overflow-x-auto">
      <table className="min-w-full border-collapse text-[10px]">
        <thead>
          <tr>
            {data.columns.map((c) => (
              <th key={c} className="border border-line px-1 py-0.5 text-left font-mono">{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.rows.map((r, i) => (
            <tr key={i}>
              {r.map((cell, j) => (
                <td key={j} className="border border-line px-1 py-0.5">{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
