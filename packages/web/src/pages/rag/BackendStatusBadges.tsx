/** ファイルごとの取り込み状況バッジ(RAG-01 から rag.tsx にあったものを切り出した)。
 *
 *  意味は従来どおり「そのバックエンドに取り込めたか」で、変えていない。
 *  「そのバックエンドで何ができるか」は BackendCapabilityPanel の担当（別物）。
 *  RAGM-03 で adb を選べるようにしたので、adb の取り込み状況もここに出る。
 */
import { usePrefs } from '../../prefs'
import type { BackendStatus, RagBackend } from './capabilityCatalog'

const beBadge: Record<BackendStatus, string> = {
  indexed: 'bg-pill-ok text-pill-ok-ink',
  pending: 'bg-band-chip/20 text-ink-muted',
  error: 'bg-primary-soft text-primary-strong',
  disabled: 'bg-band-chip/10 text-ink-muted/50',
}
const beMark: Record<BackendStatus, string> = {
  indexed: '✓', pending: '⏳', error: '!', disabled: '–',
}
// 表示順はバックエンド選択プルダウンと揃える。
const BACKEND_LABELS: { key: RagBackend; short: string }[] = [
  { key: 'vector_store', short: 'VS' },
  { key: 'adb', short: 'ADB' },
  { key: 'select_ai', short: 'SAI' },
  { key: 'opensearch', short: 'OS' },
]

export function BackendStatusBadges(
  { backends }: { backends: Partial<Record<RagBackend, BackendStatus>> },
) {
  const { t } = usePrefs()
  return (
    <span className="flex shrink-0 gap-1">
      {BACKEND_LABELS.map((b) => {
        const st = backends[b.key]
        if (!st) return null  // そのバックエンドを知らない応答(前方互換)
        return (
          <span
            key={b.key}
            className={`rounded-full px-1.5 py-0.5 text-[10px] ${beBadge[st]}`}
            title={`${t(`rag.be.${b.key}`)}: ${t(`rag.bestatus.${st}`)}`}
          >
            {beMark[st]} {b.short}
          </span>
        )
      })}
    </span>
  )
}
