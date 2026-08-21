/** 場面のタイムライン(specs/20 §6 / 要求7)。
 *
 *  **帯を選ぶとその時刻へ移動する**。これが「映像全体を最初から確認しなくても
 *  目的の場面へ直接移動できる」という機能の主要な価値そのものなので、帯は
 *  ボタンとして作る(キーボードでも辿れる。押せない帯は移動できない帯と同じ)。
 */
import { usePrefs } from '../../prefs'
import { formatRange, formatTimecode, sceneBands, type BandSource } from './format'

export function Timeline<T extends BandSource>({
  scenes, durationMs, currentMs, selectedId, onPick,
}: {
  scenes: T[]
  durationMs: number | null | undefined
  /** 再生位置(ミリ秒)。どの場面を見ているかを線で示す */
  currentMs: number
  selectedId: string | null
  onPick: (scene: T) => void
}) {
  const { t } = usePrefs()
  const bands = sceneBands(scenes, durationMs)
  const total = Math.max(Number(durationMs) || 0, ...scenes.map((s) => Number(s.end_ms) || 0), 0)
  const cursorPct = total > 0 ? Math.min(100, Math.max(0, (currentMs / total) * 100)) : 0

  if (!bands.length) {
    return <p className="text-xs text-ink-muted">{t('videos.detail.noScenes')}</p>
  }

  return (
    <div>
      <div className="relative h-11 w-full overflow-hidden rounded-rw border border-line bg-bg">
        {bands.map(({ scene, leftPct, widthPct, color }) => {
          const selected = scene.id === selectedId
          return (
            <button
              key={scene.id}
              type="button"
              onClick={() => onPick(scene)}
              data-testid="timeline-band"
              data-scene-id={scene.id}
              title={`${formatRange(scene.start_ms, scene.end_ms)}${
                scene.scene_kind && scene.scene_kind !== 'unknown' ? ` / ${scene.scene_kind}` : ''
              }`}
              aria-label={`${t('videos.detail.seek')} ${formatTimecode(scene.start_ms)}`}
              aria-pressed={selected}
              style={{
                left: `${leftPct}%`,
                width: `${widthPct}%`,
                backgroundColor: color,
                opacity: selected ? 1 : 0.72,
              }}
              className="absolute top-0 h-full border-r border-surface/60 transition-opacity hover:opacity-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-action"
            />
          )
        })}
        {/* 再生位置 */}
        <span
          aria-hidden
          style={{ left: `${cursorPct}%` }}
          className="pointer-events-none absolute top-0 h-full w-0.5 bg-ink"
        />
      </div>
      <div className="mt-1 flex items-center justify-between text-[10px] tabular-nums text-ink-muted">
        <span>0:00</span>
        <span>{t('videos.detail.timelineHint')}</span>
        <span>{formatTimecode(total)}</span>
      </div>
    </div>
  )
}
