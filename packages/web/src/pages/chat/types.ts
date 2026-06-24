/** chat/ 配下で共有する型定義(chat.tsx分割: review-validation.md §5) */

export type Msg = { role: 'user' | 'assistant'; content: string; images?: string[] }

export type ToolCall = {
  kind?: string
  name: string
  label: string
  arguments: string
  call_id?: string
  item?: Record<string, unknown>
  status?: string
  builtin?: boolean
}

export type ToolResultPair = { call: Record<string, unknown>; output: string }

export type ModelInfo = {
  key: string
  label: string
  default_temperature?: number
  api?: 'responses' | 'chat'
  reasoning?: boolean
  min_max_tokens?: number
  vision?: boolean
  multi_image?: boolean
}

export type ConvSummary = { id: string; title: string | null; model: string | null }

export type Preset = { id: string; name: string; content: string }

export type AgentDef = {
  id: string
  name: string
  icon?: string | null
  model: string
}

/** SSEストリームの data 行の形(/api/chat/stream)。 */
export type ChatStreamEvent = {
  delta?: string
  error?: string
  tool_call?: ToolCall
  tool_result?: { name: string }
  sdk_approvals?: { call_id: string; name: string; label: string; arguments: string }[]
  sdk_state?: string
}
