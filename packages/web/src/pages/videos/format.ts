/** 場面検索の画面で使う純粋関数(VID-06)。**画面から切り離して単体で検証する**。
 *
 *  ここに置くのは「時刻の見せ方」「帯の位置」「検索条件の組み立て」「?t= の解釈」——
 *  どれも間違えると *目的の場面へ移動できない* という機能の主要な価値が壊れる箇所。
 */

/** ミリ秒 → `M:SS`(1 時間以上は `H:MM:SS`)。負値・NaN は `0:00`。 */
export function formatTimecode(ms: number | null | undefined): string {
  const total = Math.floor(Math.max(0, Number(ms) || 0) / 1000)
  const s = total % 60
  const m = Math.floor(total / 60) % 60
  const h = Math.floor(total / 3600)
  const mm = h > 0 ? String(m).padStart(2, '0') : String(m)
  return `${h > 0 ? `${h}:` : ''}${mm}:${String(s).padStart(2, '0')}`
}

/** 場面の時間帯(`0:13 – 0:31`)。開始と終了を**両方**出す(要求6 の「場面」の範囲)。 */
export function formatRange(startMs: number, endMs: number): string {
  return `${formatTimecode(startMs)} – ${formatTimecode(endMs)}`
}

/** ミリ秒 → `?t=` に載せる秒(小数第1位まで。末尾の 0 は落とす)。 */
export function toSeekSeconds(ms: number | null | undefined): string {
  const sec = Math.max(0, Number(ms) || 0) / 1000
  return String(Math.round(sec * 10) / 10)
}

/** `?t=` の解釈。**読めない値は「指定なし」にする**(0 秒に丸めると先頭から再生してしまい、
 *  「その時刻から再生する」が黙って壊れたことに利用者が気づけない)。 */
export function parseSeekSeconds(raw: string | null | undefined): number | null {
  if (raw == null || raw.trim() === '') return null
  const value = Number(raw)
  if (!Number.isFinite(value) || value < 0) return null
  return value
}

/** 場面カード → 再生位置つきの詳細 URL(`/videos/{id}?t=<秒>`。specs/20 §6)。 */
export function scenePlayHref(assetId: string, startMs: number, sceneId?: string): string {
  const scene = sceneId ? `&scene=${encodeURIComponent(sceneId)}` : ''
  return `/videos/${encodeURIComponent(assetId)}?t=${toSeekSeconds(startMs)}${scene}`
}

/* --- タイムライン ------------------------------------------------------------ */

/** 種別の色。**同じ種別は必ず同じ色**にする(色分けが意味を持つのはそのときだけ)。
 *  Tailwind のクラス名ではなく実値を返す —— 動的に組み立てたクラス名は
 *  ビルド時の抽出に載らず、色が付かない。 */
const BAND_COLORS = [
  '#C74634', '#3F6B7A', '#7A6B3F', '#3F6B4F',
  '#6B4F7A', '#8A5A3C', '#4C5A8A', '#7A4F5F',
]
const UNKNOWN_BAND_COLOR = '#9A938C'

export function sceneKindColor(kind: string | null | undefined): string {
  const key = (kind ?? '').trim()
  if (!key || key === 'unknown') return UNKNOWN_BAND_COLOR
  let hash = 0
  for (const ch of key) hash = (hash * 31 + ch.codePointAt(0)!) % 100000
  return BAND_COLORS[hash % BAND_COLORS.length]
}

export type BandSource = {
  id: string
  start_ms: number
  end_ms: number
  scene_kind?: string | null
}

export type Band<T extends BandSource> = {
  scene: T
  /** タイムライン全体を 100 とした位置と幅(%) */
  leftPct: number
  widthPct: number
  color: string
}

/** 帯の最小幅(%)。**短い場面を消さない** —— 幅 0 の帯は押せない = その場面へ移動できない。 */
const MIN_BAND_PCT = 0.6

/** 場面 → タイムラインの帯。全長は映像の尺と場面の終端の**大きいほう**を使う。
 *
 *  尺は登録時に判らないことがあり(`duration_ms` は NULL 可)、分析後に埋まる。
 *  尺だけを分母にすると、NULL の間はすべての帯が消え、尺が場面より短いと帯がはみ出す。
 */
export function sceneBands<T extends BandSource>(
  scenes: T[], durationMs: number | null | undefined,
): Band<T>[] {
  const ends = scenes.map((s) => Number(s.end_ms) || 0)
  const total = Math.max(Number(durationMs) || 0, ...ends, 0)
  if (total <= 0) return []
  return scenes.map((scene) => {
    const start = Math.min(Math.max(Number(scene.start_ms) || 0, 0), total)
    const end = Math.min(Math.max(Number(scene.end_ms) || 0, start), total)
    // **幅を先に決め、はみ出すぶんは左へ寄せる。** 幅のほうを削ると、映像の末尾にある
    // 短い場面(1 時間の映像の最後の 0.1 秒など)が押せない細さになる —— 押せない帯は
    // その場面へ移動できないのと同じ
    const widthPct = Math.min(Math.max(((end - start) / total) * 100, MIN_BAND_PCT), 100)
    const leftPct = Math.min((start / total) * 100, 100 - widthPct)
    return { scene, leftPct, widthPct, color: sceneKindColor(scene.scene_kind) }
  })
}

/* --- 検索条件 ---------------------------------------------------------------- */

export type TriState = '' | 'true' | 'false'

/** 条件パネルの入力(すべて文字列。フォームが持つ形そのまま)。 */
export type SearchForm = {
  q: string
  collection: string
  category: string
  place: string
  rights: string
  indoor: string
  time_of_day: string
  analysis_state: string
  has_people: TriState
  confirmed: TriState
  tags: string
  captured_from: string
  captured_to: string
  created_from: string
  created_to: string
  duration_min_sec: string
  duration_max_sec: string
  limit: number
}

export const EMPTY_SEARCH_FORM: SearchForm = {
  q: '', collection: '', category: '', place: '', rights: '',
  indoor: '', time_of_day: '', analysis_state: '',
  has_people: '', confirmed: '', tags: '',
  captured_from: '', captured_to: '', created_from: '', created_to: '',
  duration_min_sec: '', duration_max_sec: '', limit: 20,
}

/** 1 回の検索で送れるタグ数(API の `VideoSearchFilters.tags` と同じ上限)。 */
export const TAGS_MAX = 10

/** 入力が受け取れないことを画面に伝える。**黙って条件を落とさない** ——
 *  絞り込めたつもりで別のものを見ることになる(API 側 `SearchInputError` と同じ考え)。 */
export class SearchFormError extends Error {
  readonly field: keyof SearchForm

  constructor(field: keyof SearchForm) {
    super(`invalid value for ${field}`)
    this.name = 'SearchFormError'
    this.field = field
  }
}

/** `10, 屋外  雨` → `['10', '屋外', '雨']`。区切りは読点・カンマ・空白。 */
export function splitTags(raw: string): string[] {
  return raw.split(/[,、\s]+/u).map((t) => t.trim()).filter(Boolean)
}

function seconds(raw: string, field: keyof SearchForm): number | undefined {
  const text = raw.trim()
  if (!text) return undefined
  const value = Number(text)
  if (!Number.isFinite(value) || value < 0) throw new SearchFormError(field)
  return Math.round(value * 1000)
}

export type SearchBody = {
  q?: string
  filters?: Record<string, unknown>
  similar_to_scene_id?: string
  limit: number
}

/** フォーム → `POST /api/video/search` の本文。
 *
 *  **未入力の欄は送らない**(空文字は API 側で「指定なし」に寄るが、送らないほうが
 *  「効いた条件」= 根拠の材料が正しくなる)。数値として読めない尺は `SearchFormError`。
 */
export function buildSearchBody(
  form: SearchForm, opts: { similarToSceneId?: string | null } = {},
): SearchBody {
  const filters: Record<string, unknown> = {}
  const put = (key: string, value: string) => {
    if (value.trim()) filters[key] = value.trim()
  }
  put('collection', form.collection)
  put('category', form.category)
  put('place', form.place)
  put('rights', form.rights)
  put('indoor', form.indoor)
  put('time_of_day', form.time_of_day)
  put('analysis_state', form.analysis_state)
  put('captured_from', form.captured_from)
  put('captured_to', form.captured_to)
  put('created_from', form.created_from)
  put('created_to', form.created_to)
  if (form.has_people) filters.has_people = form.has_people === 'true'
  if (form.confirmed) filters.confirmed = form.confirmed === 'true'
  const tags = splitTags(form.tags)
  if (tags.length > TAGS_MAX) throw new SearchFormError('tags')
  if (tags.length) filters.tags = tags
  const min = seconds(form.duration_min_sec, 'duration_min_sec')
  const max = seconds(form.duration_max_sec, 'duration_max_sec')
  if (min !== undefined) filters.duration_min_ms = min
  if (max !== undefined) filters.duration_max_ms = max

  const body: SearchBody = { limit: form.limit }
  const q = form.q.trim()
  // 類似検索と検索語は同時に送れない(API が 422)。類似が指定されていればそちらを優先し、
  // 検索語は載せない —— 送って 422 になるより、何で引いたかを画面で示すほうがよい
  if (opts.similarToSceneId) body.similar_to_scene_id = opts.similarToSceneId
  else if (q) body.q = q
  if (Object.keys(filters).length) body.filters = filters
  return body
}

/** 条件が 1 つでも入っているか(「条件なし = 一覧」を画面に出し分けるため)。 */
export function hasAnyCondition(form: SearchForm): boolean {
  const body = (() => {
    try {
      return buildSearchBody(form)
    } catch {
      return null
    }
  })()
  return !!body && (!!body.q || !!body.filters)
}

/** 場面の項目名 → 画面のラベル。**辞書に無い項目は名前をそのまま出す** ——
 *  `videos.field.xxx` のようなキー文字列を利用者に見せない(履歴は API が返す項目名を
 *  そのまま並べるので、項目が増えたときにここが破れる)。 */
export function fieldLabel(translate: (key: string) => string, field: string): string {
  const key = `videos.field.${field}`
  const text = translate(key)
  return text === key ? field : text
}

/* --- 台帳の JSON 列 ----------------------------------------------------------- */

/** 詳細 API が**文字列のまま**返す JSON 列を配列として読む。壊れていれば空
 *  (画面が落ちるより、その項目だけ出ないほうがよい)。 */
export function asStringList(value: unknown): string[] {
  if (Array.isArray(value)) return value.filter((v): v is string => typeof v === 'string')
  if (typeof value !== 'string' || !value.trim()) return []
  try {
    const parsed: unknown = JSON.parse(value)
    return Array.isArray(parsed)
      ? parsed.filter((v): v is string => typeof v === 'string')
      : []
  } catch {
    return []
  }
}

/** 同じく、`people` のような JSON オブジェクト列。 */
export function asObject(value: unknown): Record<string, unknown> | null {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return value as Record<string, unknown>
  }
  if (typeof value !== 'string' || !value.trim()) return null
  try {
    const parsed: unknown = JSON.parse(value)
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null
  } catch {
    return null
  }
}

/** `2026-08-19T10:00:00Z` → `2026-08-19 10:00`(UTC のまま出す。API が UTC で返すため)。 */
export function formatUtc(value: string | null | undefined): string {
  if (!value) return ''
  const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(value)
  return m ? `${m[1]} ${m[2]}` : value
}
