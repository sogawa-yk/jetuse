/** 結果テーブルのチャート描画(SQL-03)。Chart.jsは動的importでチャンク分離 */
import { useEffect, useRef } from 'react'
import type { Chart } from 'chart.js'

export type ChartSpec = {
  type: 'bar' | 'line' | 'pie' | 'none'
  x?: string | null
  y?: string[]
  title?: string
  reason?: string
}

const PALETTE = [
  '#2a6e5a', '#c74634', '#577346', '#8a6d3b', '#3b6e8a',
  '#7a4f8a', '#b08c2e', '#508a7d', '#a35252', '#6b7280',
]

function toNumber(v: string): number | null {
  const n = Number(String(v).replace(/,/g, ''))
  return Number.isFinite(n) ? n : null
}

export function ResultChart({
  spec,
  columns,
  rows,
}: {
  spec: ChartSpec
  columns: string[]
  rows: string[][]
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const chartRef = useRef<Chart | null>(null)

  useEffect(() => {
    if (spec.type === 'none' || !spec.x || !spec.y?.length) return
    let alive = true
    ;(async () => {
      const { Chart: ChartJs, registerables } = await import('chart.js')
      ChartJs.register(...registerables)
      if (!alive || !canvasRef.current) return
      const xi = columns.indexOf(spec.x as string)
      const yis = (spec.y ?? []).map((c) => columns.indexOf(c)).filter((i) => i >= 0)
      // pieは上位10件、bar/lineは先頭50行に制限
      const limit = spec.type === 'pie' ? 10 : 50
      const data = rows.slice(0, limit)
      const labels = data.map((r) => r[xi])
      const datasets =
        spec.type === 'pie'
          ? [
              {
                data: data.map((r) => toNumber(r[yis[0]]) ?? 0),
                backgroundColor: data.map((_, i) => PALETTE[i % PALETTE.length]),
              },
            ]
          : yis.map((yi, di) => ({
              label: columns[yi],
              data: data.map((r) => toNumber(r[yi])),
              backgroundColor: PALETTE[di % PALETTE.length],
              borderColor: PALETTE[di % PALETTE.length],
              fill: false,
            }))
      chartRef.current?.destroy()
      chartRef.current = new ChartJs(canvasRef.current, {
        type: spec.type as 'bar' | 'line' | 'pie', // noneは冒頭でreturn済み
        data: { labels, datasets },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            title: { display: !!spec.title, text: spec.title ?? '' },
            legend: { display: spec.type === 'pie' || (spec.y?.length ?? 0) > 1 },
          },
        },
      })
    })()
    return () => {
      alive = false
      chartRef.current?.destroy()
      chartRef.current = null
    }
  }, [spec, columns, rows])

  if (spec.type === 'none') return null
  return (
    <div className="h-80 w-full">
      <canvas ref={canvasRef} />
    </div>
  )
}
