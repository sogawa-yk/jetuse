/** 映像の場面検索(VID-06 / specs/20 §6)で画面が扱う型。
 *
 *  **JSON 列の形が入口で違う**ことに注意する。検索(`POST /api/video/search`)は
 *  tags / objects / actions を配列にして返すが、詳細(`GET /api/video/assets/{id}`)は
 *  台帳の CLOB を**文字列のまま**返す(`jetuse_core.video.row_to_scene` —— 壊れた値を
 *  API 全体の 500 に変えないため)。画面側は `asStringList` で吸収する。
 */

export type AnalysisState = 'pending' | 'running' | 'done' | 'failed' | 'partial'

/** 場面の出所(ADR-0032 決定5)。誰の言葉かを画面でも消さない。 */
export type SceneSource = 'ai' | 'human' | 'ai_confirmed'

export type VideoAsset = {
  id: string
  title: string | null
  created_at: string | null
  captured_at: string | null
  duration_ms: number | null
  collection: string | null
  category: string | null
  rights: string | null
  analysis_state: AnalysisState | null
  analysis_error: string | null
  vision_state: string | null
  thumb_object: string | null
  summary: string | null
}

/** 詳細が返す場面(JSON 列は文字列のまま)。 */
export type VideoScene = {
  id: string
  start_ms: number
  end_ms: number
  description: string | null
  tags: unknown
  objects: unknown
  people: unknown
  actions: unknown
  place: string | null
  scene_kind: string | null
  indoor: string | null
  time_of_day: string | null
  weather: string | null
  screen_text: string | null
  thumb_object: string | null
  source: SceneSource | null
  confirmed_at: string | null
}

export type VideoAssetDetail = VideoAsset & { scenes: VideoScene[] }

/** 検索が返す根拠(要求11)。**理由は必ず入る** —— 空になる経路を API が作らない。 */
export type Matched = {
  reason: string
  fields: string[]
  tags: string[]
  distance: number | null
}

/** 検索が返す 1 件 = **場面**(映像ではない。specs/20 §4)。 */
export type SearchHit = {
  scene_id: string
  asset_id: string
  title: string | null
  start_ms: number
  end_ms: number
  thumb_url: string | null
  description: string | null
  tags: string[]
  objects: string[]
  actions: string[]
  people: { present?: string; count?: number } | null
  place: string | null
  scene_kind: string | null
  indoor: string | null
  time_of_day: string | null
  weather: string | null
  screen_text: string | null
  source: SceneSource | null
  confirmed_at: string | null
  matched: Matched
  asset: {
    collection: string | null
    category: string | null
    rights: string | null
    captured_at: string | null
    created_at: string | null
    duration_ms: number | null
    analysis_state: AnalysisState | null
  }
}

export type SearchResult = {
  mode: 'vector' | 'filter' | 'similar'
  hits: SearchHit[]
  /** 全一致件数(順位を付けられた場面)。**返した件数ではない** */
  total: number
  returned: number
  /** 条件に一致したが、まだベクトルが無くて順位を付けられなかった件数 */
  excluded_no_vector: number
}

export type SceneEdit = {
  field: string
  before: string | null
  after: string | null
  edited_by: string | null
  edited_at: string | null
}
