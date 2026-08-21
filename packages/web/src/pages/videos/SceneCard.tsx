/** 検索結果の**場面カード**(specs/20 §6 / 要求6・11)。
 *
 *  カードは 2 つの約束を守る:
 *   1. **一致理由を必ず出す**(要求11)。AI 検索をブラックボックスにしない。
 *   2. **その場面へ移動できる**。カードは `/videos/{id}?t=<秒>` へのリンクそのもの。
 */
import { Link } from 'react-router-dom'
import { usePrefs } from '../../prefs'
import { fieldLabel, formatRange, formatTimecode, formatUtc, scenePlayHref } from './format'
import type { SearchHit } from './types'

/** 根拠の `fields`(機械が使う列名)→ 画面のラベル。未知の列名はそのまま出す
 *  (**捏造も黙殺もしない**)。 */
function FieldChips({ fields }: { fields: string[] }) {
  const { t } = usePrefs()
  if (!fields.length) return null
  const label = (f: string) => fieldLabel((k) => t(k as Parameters<typeof t>[0]), f)
  return (
    <div className="mt-1 flex flex-wrap items-center gap-1">
      <span className="text-[10px] text-ink-muted">{t('videos.reason.fields')}:</span>
      {fields.map((f) => (
        <span
          key={f}
          className="rounded-full border border-line bg-bg px-2 py-0.5 text-[10px] text-ink-muted"
        >
          {label(f)}
        </span>
      ))}
    </div>
  )
}

export function SceneCard({
  hit, onSimilar,
}: { hit: SearchHit; onSimilar?: (sceneId: string) => void }) {
  const { t } = usePrefs()
  const href = scenePlayHref(hit.asset_id, hit.start_ms, hit.scene_id)
  const asset = hit.asset

  return (
    <article className="flex flex-col overflow-hidden rounded-rw-lg bg-surface shadow-rw">
      <Link to={href} className="group block" aria-label={t('videos.play')}>
        <div className="relative aspect-video w-full bg-bg">
          {hit.thumb_url ? (
            <img
              src={hit.thumb_url}
              alt=""
              loading="lazy"
              className="h-full w-full object-cover"
            />
          ) : (
            <div className="flex h-full w-full items-center justify-center text-xs text-ink-muted/60">
              {t('videos.noThumb')}
            </div>
          )}
          {/* 場面の時間帯。**開始だけでなく終了も出す**(どこからどこまでの場面か) */}
          <span className="absolute bottom-1 right-1 rounded-rw bg-header/80 px-1.5 py-0.5 text-[11px] tabular-nums text-header-ink">
            {formatRange(hit.start_ms, hit.end_ms)}
          </span>
        </div>
      </Link>

      <div className="flex min-w-0 flex-1 flex-col gap-2 p-3">
        <div className="min-w-0">
          <Link
            to={href}
            className="block truncate text-sm font-bold hover:text-action"
            title={hit.title ?? ''}
          >
            {hit.title || hit.asset_id}
          </Link>
          {/* 映像の基本情報(specs/20 §6 の場面カード) */}
          <p className="mt-0.5 truncate text-[11px] text-ink-muted">
            {[
              asset.collection,
              asset.category,
              asset.captured_at ? formatUtc(asset.captured_at) : '',
              asset.duration_ms ? formatTimecode(asset.duration_ms) : '',
            ].filter(Boolean).join(' ・ ')}
          </p>
        </div>

        <p className="line-clamp-3 text-xs leading-relaxed text-ink">
          {hit.description}
        </p>

        {hit.tags.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {hit.tags.slice(0, 8).map((tag) => (
              <span
                key={tag}
                className={`rounded-full px-2 py-0.5 text-[10px] ${
                  hit.matched.tags.includes(tag)
                    ? 'bg-action-soft text-ink'
                    : 'border border-line bg-bg text-ink-muted'
                }`}
              >
                {tag}
              </span>
            ))}
          </div>
        )}

        {/* --- 一致理由(要求11)。**必ず出す** --- */}
        <div
          className="rounded-rw border border-line bg-bg px-2.5 py-2"
          data-testid="scene-reason"
        >
          <div className="flex items-center gap-2">
            <span className="text-[10px] font-bold text-ink-muted">{t('videos.reason')}</span>
            {hit.matched.distance != null && (
              <span className="ml-auto text-[10px] tabular-nums text-ink-muted">
                {t('videos.distance')} {hit.matched.distance}
              </span>
            )}
          </div>
          <p className="mt-1 text-[11px] leading-relaxed text-ink">{hit.matched.reason}</p>
          <FieldChips fields={hit.matched.fields} />
        </div>

        <div className="mt-auto flex items-center gap-2 pt-1">
          <Link
            to={href}
            className="rounded-rw bg-cta px-3 py-1.5 text-xs font-medium text-cta-ink hover:bg-cta-strong"
          >
            ▶ {t('videos.play')}
          </Link>
          {onSimilar && (
            <button
              type="button"
              onClick={() => onSimilar(hit.scene_id)}
              className="rounded-rw border border-line px-3 py-1.5 text-xs text-ink-muted hover:border-action hover:text-action"
            >
              {t('videos.similar')}
            </button>
          )}
        </div>
      </div>
    </article>
  )
}
