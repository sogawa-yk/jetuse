/** DBチャットのNL2SQLストリーミング(dbchat.tsx分割: review-validation.md §7)。
 *  質問→/api/chat/nl2sql のSSE(sql/error)を readSse で受信し、生成中の経過秒も保持する。 */
import { useRef, useState } from 'react'
import { authHeaders, reauthenticate, type User } from '../../auth'
import { readSse } from '../../lib/sse'

export type Nl2SqlBackend = 'sql_search' | 'select_ai'
export type Nl2SqlTarget = 'sample' | 'datasets'

export type UseDbChatStreamArgs = {
  user: User
  /** 401時の i18n メッセージ(uc.sessionLost)。 */
  t: (key: 'uc.sessionLost') => string
  /** 生成したSQLを画面状態へ反映する。 */
  onSql: (sql: string) => void
  /** ストリーム中のエラー文言を画面状態へ反映する。 */
  onError: (msg: string) => void
}

export function useDbChatStream({ user, t, onSql, onError }: UseDbChatStreamArgs) {
  const [generating, setGenerating] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const abortRef = useRef<AbortController | null>(null)

  const generate = async (
    question: string, backend: Nl2SqlBackend, target: Nl2SqlTarget, model?: string,
  ) => {
    const q = question.trim()
    if (!q || generating) return
    setGenerating(true)
    setElapsed(0)
    const timer = setInterval(() => setElapsed((s) => s + 1), 1000)
    const ac = new AbortController()
    abortRef.current = ac
    try {
      const res = await fetch('/api/chat/nl2sql', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
        // model は Select AI のモデル選択(feedback 20260620 #3)。未指定時はサーバ既定。
        body: JSON.stringify({ question: q, backend, target, model: model || null }),
        signal: ac.signal,
      })
      if (res.status === 401) {
        reauthenticate()
        throw new Error(t('uc.sessionLost'))
      }
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)
      await readSse<{ sql?: string; error?: string }>(
        res,
        (ev) => {
          if (ev.sql) onSql(ev.sql)
          if (ev.error) onError(ev.error)
        },
        { signal: ac.signal },
      )
    } catch (e) {
      const aborted = e instanceof DOMException && e.name === 'AbortError'
      if (!aborted) onError(String(e instanceof Error ? e.message : e))
    } finally {
      clearInterval(timer)
      setGenerating(false)
      abortRef.current = null
    }
  }

  const stop = () => abortRef.current?.abort()

  return { generating, elapsed, generate, stop }
}
