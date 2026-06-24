/** 会話一覧・履歴の読み書き(chat.tsx分割: review-validation.md §5)。
 *  convs(一覧) / currentId(選択中) と、作成・選択・削除・新規 を担う。
 *  メッセージ本体は useChatStream 側が保持するため setMsgs を受け取って書き込む。 */
import { useState } from 'react'
import { authHeaders, type User } from '../../auth'
import type { ConvSummary, Msg } from './types'

export type UseConversationsArgs = {
  user: User
  busy: boolean
  /** 会話選択時に履歴メッセージを流し込む(useChatStreamのsetMsgs)。 */
  setMsgs: (msgs: Msg[]) => void
  /** 会話選択時に保存モデルへ切り替える。 */
  setModel: (model: string) => void
  /** モバイルで会話選択時に履歴サイドバーを閉じる等のUI連動。 */
  onSelect?: () => void
}

export function useConversations({ user, busy, setMsgs, setModel, onSelect }: UseConversationsArgs) {
  const [convs, setConvs] = useState<ConvSummary[]>([])
  const [currentId, setCurrentId] = useState<string | null>(null)

  const loadConvs = () =>
    fetch('/api/conversations', { headers: authHeaders(user) })
      .then((r) => r.json())
      .then((d) => setConvs(d.conversations))
      .catch(() => setConvs([]))

  const newChat = () => {
    if (busy) return
    setMsgs([])
    setCurrentId(null)
  }

  const selectConv = async (id: string) => {
    if (busy) return
    const res = await fetch(`/api/conversations/${id}`, { headers: authHeaders(user) })
    if (!res.ok) return
    const d = await res.json()
    setCurrentId(id)
    setMsgs(d.messages)
    if (d.model) setModel(d.model)
    onSelect?.()
  }

  const deleteConv = async (id: string) => {
    await fetch(`/api/conversations/${id}`, {
      method: 'DELETE',
      headers: authHeaders(user),
    })
    if (id === currentId) newChat()
    void loadConvs()
  }

  /** 初回送信時に会話を作成する。DB障害時は null を返し、呼び出し側で通知する(CHAT-07)。
   *  作成できたら currentId に反映する。 */
  const createConversation = async (model: string, title: string): Promise<string | null> => {
    try {
      const res = await fetch('/api/conversations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
        body: JSON.stringify({ model, title }),
        signal: AbortSignal.timeout(15000),
      })
      if (res.ok) {
        const cid = (await res.json()).id as string
        setCurrentId(cid)
        return cid
      }
      return null
    } catch {
      return null // 永続化できなくてもチャットは継続
    }
  }

  return {
    convs,
    currentId,
    setCurrentId,
    loadConvs,
    newChat,
    selectConv,
    deleteConv,
    createConversation,
  }
}
