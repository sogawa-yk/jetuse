/** /api/chat/stream へ送るリクエストボディの構築(純粋関数・テスト容易)。
 *  chat.tsx の stream() に直書きされていた payload 生成を切り出したもの
 *  (review-validation.md §5: chat payload構築をVitestテストしてからUI分割)。 */
import type { Msg, ToolResultPair } from './types'

export type BuildChatRequestParams = {
  model: string
  /** 画面に表示している会話履歴(systemPromptは含めない)。 */
  history: Msg[]
  /** 送信時のみ先頭に付与するシステムプロンプト(空なら付与しない)。 */
  systemPrompt: string
  temperature: number
  /** top_p。1(=モデル既定)のときは送信しない(null)。 */
  topP: number
  /** max_tokens の文字列入力。空なら null。 */
  maxTokens: string
  /** reasoningモデルのみ有効な努力度。 */
  effort: string
  isReasoning: boolean
  conversationId: string | null
  persistUser: boolean
  /** 保存済みカスタムエージェントの定義(あればアドホックagentモードは無効)。 */
  agentDefId: string | null
  selectedTools: string[]
  selectedMcp: string[]
  autoTools: boolean
  toolResults: ToolResultPair[] | null
  /** 当該ターンのみ送る画像(MM-01)。 */
  sendImages: string[] | null
  /** Agents SDK 承認往復の再開(FW-01b)。 */
  sdkResume: { state: string; approvals: Record<string, boolean> } | null
}

export type ChatRequestBody = {
  model: string
  messages: { role: string; content: string }[]
  temperature: number
  top_p: number | null
  max_tokens: number | null
  reasoning_effort: string | null
  conversation_id: string | null
  persist_user: boolean
  agent: boolean
  agent_id: string | null
  auto_tools: boolean
  tool_results: ToolResultPair[] | null
  enabled_tools: string[] | null
  mcp_server_ids: string[] | null
  images: string[] | null
  sdk_state: string | null
  sdk_approvals: Record<string, boolean> | null
}

export function buildChatRequest(p: BuildChatRequestParams): ChatRequestBody {
  const trimmedSystem = p.systemPrompt.trim()
  return {
    model: p.model,
    // システムプロンプトは送信時のみ先頭に付与(画面/ADBの履歴には含めない)
    messages: (trimmedSystem
      ? [{ role: 'system', content: trimmedSystem }, ...p.history]
      : p.history
    ).map((m) => ({ role: m.role, content: m.content })),
    temperature: p.temperature,
    top_p: p.topP < 1 ? p.topP : null,
    max_tokens: p.maxTokens.trim() ? Number(p.maxTokens) : null,
    reasoning_effort: p.isReasoning && p.effort ? p.effort : null,
    conversation_id: p.conversationId,
    persist_user: p.persistUser,
    agent: !p.agentDefId && (p.selectedTools.length > 0 || p.selectedMcp.length > 0) && p.isReasoning,
    agent_id: p.agentDefId,
    auto_tools: p.autoTools,
    tool_results: p.toolResults,
    enabled_tools: p.selectedTools.length > 0 ? p.selectedTools : null,
    mcp_server_ids: p.selectedMcp.length > 0 ? p.selectedMcp : null,
    images: p.sendImages && p.sendImages.length > 0 ? p.sendImages : null,
    sdk_state: p.sdkResume?.state ?? null,
    sdk_approvals: p.sdkResume?.approvals ?? null,
  }
}
