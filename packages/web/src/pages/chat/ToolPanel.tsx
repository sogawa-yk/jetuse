/** ツール承認カード(chat.tsx分割: review-validation.md §5)。
 *  エージェントがツール実行の承認待ち(pending_approval / Agents SDK中断 FW-01b)のとき、
 *  対象ツール一覧と 実行/拒否 ボタンを表示する。承認往復(sdk_approvals)はここを起点に行う。 */
import { usePrefs } from '../../prefs'
import type { ToolCall } from './types'

export type ToolPanelProps = {
  pendingCalls: ToolCall[]
  approving: boolean
  onApprove: () => void
  onDeny: () => void
}

export function ToolPanel({ pendingCalls, approving, onApprove, onDeny }: ToolPanelProps) {
  const { t } = usePrefs()
  return (
    <div className="rounded-rw border border-action bg-action-soft p-3 text-sm">
      <p className="mb-2 font-medium">🛠 {t('chat.tool.pending')}</p>
      <ul className="mb-3 space-y-1.5">
        {pendingCalls.map((c, i) => (
          <li key={i} className="rounded-rw border border-line bg-surface px-2.5 py-1.5">
            <span className="font-medium">{c.label}</span>
            <code className="ml-2 break-all text-xs text-ink-muted">{c.arguments}</code>
          </li>
        ))}
      </ul>
      <div className="flex gap-2">
        <button
          onClick={onApprove}
          disabled={approving}
          className="rounded-rw bg-cta px-3 py-1.5 text-xs font-medium text-cta-ink hover:bg-cta-strong disabled:opacity-40"
        >
          {approving ? t('chat.tool.running') : `▶ ${t('chat.tool.approve')}`}
        </button>
        <button
          onClick={onDeny}
          disabled={approving}
          className="rounded-rw border border-line px-3 py-1.5 text-xs hover:border-action disabled:opacity-40"
        >
          {t('chat.tool.deny')}
        </button>
      </div>
    </div>
  )
}
