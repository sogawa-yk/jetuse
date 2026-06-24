/** チャットのストリーミング状態とロジック(chat.tsx分割: review-validation.md §5)。
 *
 *  msgs / busy / ツール承認(pendingCalls・approving・sdk往復) を保持し、
 *  fetch + readSse によるSSE受信、中断、再生成、承認往復を担う。
 *  リクエストボディの構築は buildChatRequest() に委譲する(純粋・テスト容易)。 */
import { useRef, useState } from 'react'
import { authHeaders, reauthenticate, type User } from '../../auth'
import { readSse } from '../../lib/sse'
import { buildChatRequest } from './buildChatRequest'
import type { ChatStreamEvent, Msg, ToolCall, ToolResultPair } from './types'

/** stream() 1回分のリクエスト設定(画面のモデル/生成パラメータ等)。 */
export type ChatStreamConfig = {
  model: string
  systemPrompt: string
  temperature: number
  topP: number
  maxTokens: string
  effort: string
  isReasoning: boolean
  agentDefId: string | null
  selectedTools: string[]
  selectedMcp: string[]
  autoTools: boolean
}

export type UseChatStreamArgs = {
  user: User
  /** i18n。空応答(chat.emptyResp)・DB障害(chat.dbDown)メッセージに使用。 */
  t: (key: 'chat.dbDown' | 'chat.emptyResp') => string
  /** ストリーム完了後に会話一覧を再読込する(タイトル自動生成の反映含む)。 */
  loadConvs: () => void
  /** 現在のリクエスト設定を返す(レンダーごとに最新を読むため関数で受ける)。 */
  getConfig: () => ChatStreamConfig
  /** ストリーム開始時に呼ばれる(送信時のスクロール追従再開などに使う)。 */
  onStreamStart?: () => void
}

export function useChatStream({ user, t, loadConvs, getConfig, onStreamStart }: UseChatStreamArgs) {
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [busy, setBusy] = useState(false)
  const [pendingCalls, setPendingCalls] = useState<ToolCall[] | null>(null)
  const [approving, setApproving] = useState(false)
  const sdkStateRef = useRef<string | null>(null) // Agents SDK承認往復(FW-01b)
  const abortRef = useRef<AbortController | null>(null)
  // 承認ラウンドをまたぐツール結果の累積(AGT-01d: 送らないとモデルが結果を忘れてループする)
  const turnResultsRef = useRef<ToolResultPair[]>([])

  const appendToAssistant = (text: string) =>
    setMsgs((cur) => {
      const next = [...cur]
      const last = next[next.length - 1]
      if (last?.role === 'assistant') {
        next[next.length - 1] = { ...last, content: last.content + text }
      }
      return next
    })

  const stream = async (
    history: Msg[],
    conversationId: string | null,
    persistUser = true,
    toolResults: ToolResultPair[] | null = null,
    sendImages: string[] | null = null, // 画像は当該ターンのみ(MM-01)
    sdkResume: { state: string; approvals: Record<string, boolean> } | null = null,
  ) => {
    const cfg = getConfig()
    setBusy(true)
    setPendingCalls(null)
    onStreamStart?.() // 送信時は追従を再開
    setMsgs([...history, { role: 'assistant', content: '' }])
    const ac = new AbortController()
    abortRef.current = ac
    let sawDelta = false
    let sawText = false // 本文テキスト(リトライ可否の基準: ツール通知のみなら再実行safe)
    const approvalBuffer: ToolCall[] = []
    // コンテンツ受信前のネットワーク断は1回だけ自動リトライ(GW経由の間欠切断対策)
    try {
    for (let attempt = 1; attempt <= 2; attempt++) {
    try {
      const res = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
        body: JSON.stringify(
          buildChatRequest({
            model: cfg.model,
            history,
            systemPrompt: cfg.systemPrompt,
            temperature: cfg.temperature,
            topP: cfg.topP,
            maxTokens: cfg.maxTokens,
            effort: cfg.effort,
            isReasoning: cfg.isReasoning,
            conversationId,
            persistUser,
            agentDefId: cfg.agentDefId,
            selectedTools: cfg.selectedTools,
            selectedMcp: cfg.selectedMcp,
            autoTools: cfg.autoTools,
            toolResults,
            sendImages,
            sdkResume,
          }),
        ),
        signal: ac.signal,
      })
      if (res.status === 401) {
        // トークン失効: 再ログインへ(Domainセッション残存時は無操作で復帰)
        reauthenticate()
        throw new Error('セッションの有効期限が切れました。再ログインします…')
      }
      if (res.status === 503) throw new Error(t('chat.dbDown'))
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)
      await readSse<ChatStreamEvent>(
        res,
        (ev) => {
          if (ev.delta) {
            sawDelta = true
            sawText = true
            appendToAssistant(ev.delta)
          }
          if (ev.tool_call) {
            sawDelta = true
            if (ev.tool_call.status === 'pending_approval') {
              approvalBuffer.push(ev.tool_call)
            } else {
              appendToAssistant(`\n\n> 🛠 ${ev.tool_call.label} を実行中…\n\n`)
            }
          }
          if (ev.tool_result) {
            appendToAssistant(`> ✓ ${ev.tool_result.name} 完了\n\n`)
          }
          if (ev.sdk_approvals && ev.sdk_state) {
            // Agents SDKの承認中断(FW-01b): 既存の承認カードUIに載せる
            sawDelta = true
            sdkStateRef.current = ev.sdk_state
            for (const a of ev.sdk_approvals) {
              approvalBuffer.push({
                kind: 'sdk', name: a.name, label: a.label,
                arguments: a.arguments, call_id: a.call_id,
              })
            }
          }
          if (ev.error) {
            sawDelta = true
            appendToAssistant(`\n\n> ⚠ ${ev.error}`)
          }
        },
        { signal: ac.signal },
      )
      // 完走したのに本文ゼロ(実測: gpt-ossはtemperature≧1.9で推論暴走し本文が出ない)
      if (!sawDelta) appendToAssistant(`> ⚠ ${t('chat.emptyResp')}`)
      if (approvalBuffer.length) setPendingCalls(approvalBuffer)
      break
    } catch (e) {
      const aborted = e instanceof DOMException && e.name === 'AbortError'
      if (aborted) break
      const networkError = e instanceof TypeError
      if (networkError && !sawText && attempt === 1) {
        // 本文未受信なら再試行safe(ツールは読み取り専用)。途中のツール通知はリセット
        setMsgs((cur) => {
          const next = [...cur]
          const last = next[next.length - 1]
          if (last?.role === 'assistant') next[next.length - 1] = { ...last, content: '' }
          return next
        })
        approvalBuffer.length = 0
        continue
      }
      appendToAssistant(
        `\n\n> ⚠ ${String(e)}${networkError ? '（接続が切断されました。再生成をお試しください）' : ''}`,
      )
      break
    }
    }
    } finally {
      setBusy(false)
      abortRef.current = null
      loadConvs()
      // 初回のやり取り完了後にタイトル自動生成(CHAT-05)
      if (conversationId && persistUser && history.filter((m) => m.role === 'user').length === 1) {
        fetch(`/api/conversations/${conversationId}/title`, {
          method: 'POST',
          headers: authHeaders(user),
        })
          .then(() => loadConvs())
          .catch(() => {})
      }
    }
  }

  // 承認フロー(AGT-01): ツール実行→結果付きで継続ストリーム
  const continueWithResults = (results: ToolResultPair[], conversationId: string | null) => {
    // ターン内の全ラウンドの結果を累積して送る(AGT-01d: ループバグ修正)
    turnResultsRef.current = [...turnResultsRef.current, ...results]
    const history = [...msgs]
    while (history.length && history[history.length - 1].role === 'assistant') history.pop()
    void stream(history, conversationId, false, turnResultsRef.current)
  }

  const resumeSdk = (approve: boolean, conversationId: string | null) => {
    // Agents SDK: サーバー側で再開実行するため可否だけ返す(FW-01b)
    if (!pendingCalls || !sdkStateRef.current) return
    const approvals: Record<string, boolean> = {}
    for (const c of pendingCalls) approvals[c.call_id ?? ''] = approve
    const state = sdkStateRef.current
    sdkStateRef.current = null
    setPendingCalls(null)
    const history = [...msgs]
    while (history.length && history[history.length - 1].role === 'assistant') history.pop()
    void stream(history, conversationId, false, null, null, { state, approvals })
  }

  const approveTools = async (conversationId: string | null) => {
    if (!pendingCalls || approving) return
    if (pendingCalls[0]?.kind === 'sdk') return resumeSdk(true, conversationId)
    setApproving(true)
    try {
      const results: ToolResultPair[] = []
      for (const c of pendingCalls) {
        if (c.kind === 'mcp') {
          // MCPはOCI側で実行されるため承認フラグのみ返す(AGT-02)
          results.push({ call: c.item ?? {}, output: 'approve' })
          continue
        }
        const res = await fetch('/api/agent/execute-tool', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
          body: JSON.stringify({ name: c.name, arguments: c.arguments }),
        })
        const data = await res.json()
        results.push({
          call: c.item ?? {},
          output: res.ok
            ? data.output
            : JSON.stringify({ error: String(data.detail ?? res.status) }),
        })
      }
      continueWithResults(results, conversationId)
    } finally {
      setApproving(false)
    }
  }

  const denyTools = (conversationId: string | null) => {
    if (!pendingCalls) return
    if (pendingCalls[0]?.kind === 'sdk') return resumeSdk(false, conversationId)
    continueWithResults(
      pendingCalls.map((c) => ({
        call: c.item ?? {},
        output: c.kind === 'mcp' ? 'deny' : '{"error": "ユーザーがツール実行を拒否しました"}',
      })),
      conversationId,
    )
  }

  const regenerate = (conversationId: string | null) => {
    if (busy) return
    turnResultsRef.current = []
    // 末尾のアシスタント応答を捨てて直前のユーザー発話から再生成
    const history = [...msgs]
    while (history.length && history[history.length - 1].role === 'assistant') history.pop()
    if (history.length) void stream(history, conversationId, false)
  }

  /** 新しいターンを開始する前に累積ツール結果をリセットする(send時)。 */
  const resetTurn = () => {
    turnResultsRef.current = []
  }

  const stop = () => abortRef.current?.abort()

  return {
    msgs,
    setMsgs,
    busy,
    pendingCalls,
    approving,
    stream,
    approveTools,
    denyTools,
    regenerate,
    resetTurn,
    stop,
  }
}
