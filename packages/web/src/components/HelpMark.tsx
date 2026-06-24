/** ヘルプマーク(feedback 20260620 #4)。タイトル横の「?」を押すと、
 *  その機能のアーキ図(構成図)とコア機能の説明をポップアップ(モーダル)で表示する。
 *  モーダルはオーバーレイのクリック/✕/Escで閉じる(layout.tsx のアカウントメニュー流儀)。 */
import { useEffect, useState } from 'react'
import { usePrefs } from '../prefs'
import { HELP_CONTENT, type HelpKey } from './helpContent'

export function HelpMark({ topic }: { topic: HelpKey }) {
  const { t } = usePrefs()
  const [open, setOpen] = useState(false)
  const entry = HELP_CONTENT[topic]
  // helpContent のキー文字列を t() の許容キー型へ寄せる(layout.tsx と同じ手法)
  const tk = (k: string) => t(k as Parameters<typeof t>[0])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open])

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label={t('help.open')}
        title={t('help.open')}
        className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-line text-xs font-bold text-ink-muted hover:border-action hover:text-action"
      >
        ?
      </button>
      {open && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-header/40 p-4"
          onClick={() => setOpen(false)}
          role="dialog"
          aria-modal="true"
          aria-label={tk(entry.titleKey)}
        >
          <div
            className="flex max-h-[88vh] w-full max-w-3xl flex-col overflow-hidden rounded-rw border border-line bg-surface shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-start justify-between gap-3 border-b border-line px-5 py-3">
              <div className="min-w-0">
                <h2 className="text-lg font-bold">{tk(entry.titleKey)}</h2>
                <p className="mt-0.5 text-xs text-ink-muted">{t('help.subtitle')}</p>
              </div>
              <button
                onClick={() => setOpen(false)}
                aria-label={t('help.close')}
                className="px-2 text-xl leading-none text-ink-muted hover:text-ink"
              >
                ✕
              </button>
            </div>
            <div className="space-y-4 overflow-y-auto px-5 py-4">
              <p className="whitespace-pre-line text-sm leading-relaxed">{tk(entry.descKey)}</p>
              <div>
                <p className="mb-1 text-xs font-medium text-ink-muted">{t('help.architecture')}</p>
                <a href={entry.diagram} target="_blank" rel="noreferrer" title={t('help.openImage')}>
                  <img
                    src={entry.diagram}
                    alt={tk(entry.titleKey)}
                    className="w-full rounded-rw border border-line bg-white"
                  />
                </a>
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
