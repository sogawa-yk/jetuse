/** RAG バックエンドの語彙と、能力差(RAGM-03 / ADR-0020 §3)の型・取り出し。
 *
 *  事実の正本は API の GET /api/capabilities（`rag.search` の `backend_capabilities`）。
 *  画面側に能力をハードコードしない — したら「未実証を『できる』と書く」経路が
 *  画面にもできてしまう。ここは型付けと取り出しだけを持つ。
 */

/** チャットで選べる RAG バックエンド(API 側 ChatRequest.rag_backend と同じ集合)。 */
export type RagBackend = 'vector_store' | 'adb' | 'select_ai' | 'opensearch'

/** 取り込み状況(そのファイルを取り込めたか)。能力差(何ができるか)とは別物。 */
export type BackendStatus = 'indexed' | 'pending' | 'error' | 'disabled'

export type SupportLevel = 'yes' | 'limited' | 'no' | 'unverified'

export type AxisKey =
  | 'citation_granularity'
  | 'filter_expressiveness'
  | 'business_data_join'
  | 'row_level_security'
  | 'metadata_update_consistency'

export type AxisEntry = {
  support: SupportLevel
  verified: boolean
  detail: string
  evidence: string
}

export type BackendCapability = {
  label: string
  role: string
  axes: Partial<Record<AxisKey, AxisEntry>>
  notes?: string[]
}

export type RagBackendCapabilities = {
  axes: AxisKey[]
  backends: Record<string, BackendCapability>
}

const isObj = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v)

/** GET /api/capabilities のレスポンスから rag.search の能力差を取り出す。
 *  形が違えば null(＝パネルを出さない)。能力表が無くても RAG チャット自体は動く。 */
export function pickRagBackendCapabilities(payload: unknown): RagBackendCapabilities | null {
  if (!isObj(payload) || !Array.isArray(payload.capabilities)) return null
  const cap = payload.capabilities.find(
    (c: unknown) => isObj(c) && c.capability === 'rag.search',
  )
  if (!isObj(cap)) return null
  const bc = cap.backend_capabilities
  if (!isObj(bc) || !Array.isArray(bc.axes) || !isObj(bc.backends)) return null
  return bc as unknown as RagBackendCapabilities
}
