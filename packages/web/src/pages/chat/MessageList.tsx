/** メッセージ一覧の描画(chat.tsx分割: review-validation.md §5)。
 *  ユーザー/アシスタントの吹き出し・コピー/再生成・ツール承認カードを担う。 */
import { type RefObject } from 'react'
import { Md } from '../../components/markdown'
import { usePrefs } from '../../prefs'
import { ToolPanel } from './ToolPanel'
import type { Msg, ToolCall } from './types'

export type MessageListProps = {
  msgs: Msg[]
  busy: boolean
  pendingCalls: ToolCall[] | null
  approving: boolean
  bottomRef: RefObject<HTMLDivElement | null>
  onCopy: (text: string) => void
  onRegenerate: () => void
  onApprove: () => void
  onDeny: () => void
}

export function MessageList({
  msgs,
  busy,
  pendingCalls,
  approving,
  bottomRef,
  onCopy,
  onRegenerate,
  onApprove,
  onDeny,
}: MessageListProps) {
  const { t } = usePrefs()

  if (msgs.length === 0) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-center">
          <div className="mb-2 text-3xl">💬</div>
          <p className="font-medium">{t('chat.empty.title')}</p>
          <p className="mt-1 text-sm text-ink-muted">{t('chat.empty.body')}</p>
        </div>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      {msgs.map((m, i) =>
        m.role === 'user' ? (
          <div key={i} className="flex justify-end">
            <div className="max-w-[80%] rounded-rw rounded-tr-none bg-band px-4 py-2.5 text-sm text-band-ink">
              {m.images && m.images.length > 0 && (
                <div className="mb-2 flex flex-wrap gap-1.5">
                  {m.images.map((u, j) => (
                    <img key={j} src={u} alt="" className="h-20 rounded-rw object-cover" />
                  ))}
                </div>
              )}
              <span className="whitespace-pre-wrap">{m.content}</span>
            </div>
          </div>
        ) : (
          <div key={i} className="flex justify-start gap-2">
            <span className="mt-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-band-chip text-xs font-bold text-white">
              AI
            </span>
            <div className="md max-w-[85%] rounded-rw rounded-tl-none border border-line bg-surface px-4 py-2.5 text-sm leading-relaxed">
              <Md>{m.content || '…'}</Md>
              {(!busy || i < msgs.length - 1) && m.content && (
                <div className="mt-2 flex gap-3 text-xs text-ink-muted">
                  <button className="hover:text-action" onClick={() => onCopy(m.content)}>
                    ⧉ {t('chat.copy')}
                  </button>
                  {i === msgs.length - 1 && (
                    <button className="hover:text-action" onClick={onRegenerate}>
                      ↻ {t('chat.regenerate')}
                    </button>
                  )}
                </div>
              )}
            </div>
          </div>
        ),
      )}
      {pendingCalls && (
        <ToolPanel
          pendingCalls={pendingCalls}
          approving={approving}
          onApprove={onApprove}
          onDeny={onDeny}
        />
      )}
      <div ref={bottomRef} />
    </div>
  )
}
