/** RAGM-03: 選択中の RAG バックエンドで「何ができるか」を出す(ADR-0020 §3)。
 *
 *  ファイル右の取り込み状況バッジ(VS/SAI/OS)とは**別物**である:
 *    取り込み状況バッジ = そのファイルを取り込めたか(indexed / pending / disabled)
 *    ここ               = そのバックエンドを選ぶと何ができるか(出典粒度・絞り込み…)
 *
 *  中身(事実)は API の能力カタログが正本で、画面はそれを描くだけ(capabilityCatalog.ts)。
 */
import { usePrefs } from '../../prefs'
import type { RagBackendCapabilities, SupportLevel } from './capabilityCatalog'

const supportBadge: Record<SupportLevel, string> = {
  yes: 'bg-pill-ok text-pill-ok-ink',
  limited: 'bg-band-chip/20 text-ink-muted',
  no: 'bg-band-chip/10 text-ink-muted/60',
  unverified: 'bg-primary-soft text-primary-strong',
}
const supportMark: Record<SupportLevel, string> = {
  yes: '✓', limited: '△', no: '✕', unverified: '?',
}

export function BackendCapabilityPanel(
  { backend, caps }: { backend: string; caps: RagBackendCapabilities | null },
) {
  const { t } = usePrefs()
  const be = caps?.backends[backend]
  if (!caps || !be) return null

  return (
    <section
      className="rounded-rw border border-line bg-bg p-3"
      aria-label="backend capabilities"
      data-testid={`rag-capabilities-${backend}`}
    >
      <div className="mb-2">
        <h3 className="text-xs font-semibold text-ink-muted">{t('rag.cap.title')}</h3>
        <p className="text-[11px] text-ink-muted/80">{be.label} — {be.role}</p>
      </div>
      <ul className="space-y-1.5">
        {caps.axes.map((axis) => {
          const entry = be.axes[axis]
          if (!entry) return null
          return (
            <li key={axis} className="flex items-start gap-2 text-[11px]">
              <span
                className={`mt-px shrink-0 rounded-full px-1.5 py-0.5 ${supportBadge[entry.support]}`}
                title={entry.evidence}
              >
                {supportMark[entry.support]} {t(`rag.cap.support.${entry.support}`)}
              </span>
              <span className="min-w-0">
                <span className="font-medium">{t(`rag.cap.axis.${axis}`)}</span>
                <span className="text-ink-muted"> — {entry.detail}</span>
              </span>
            </li>
          )
        })}
      </ul>
      {be.notes && be.notes.length > 0 && (
        <ul className="mt-2 space-y-0.5">
          {be.notes.map((n) => (
            <li key={n} className="text-[10px] leading-snug text-ink-muted/80">※ {n}</li>
          ))}
        </ul>
      )}
      <p className="mt-2 text-[10px] leading-snug text-ink-muted/70">{t('rag.cap.legend')}</p>
    </section>
  )
}
