/** 映像 API(specs/20 §2 §3 §4 §5)への薄い入口。**失敗の理由を落とさない**。
 *
 *  API は 409(分析中・場面が変わった)・422(値が受け取れない)・502(上流の障害)・
 *  503(映像機能が未設定)を**理由ごとに違う番号**で返す。画面がそれを
 *  「失敗しました」に丸めると、利用者は直せるのか待てばよいのかが判らなくなる。
 */
import { authHeaders, reauthenticate, type User } from '../../auth'
import type {
  SearchResult, SceneEdit, VideoAsset, VideoAssetDetail, VideoScene,
} from './types'
import type { SearchBody } from './format'

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request<T>(user: User, path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, { ...init, headers: { ...authHeaders(user), ...init.headers } })
  if (res.status === 401) {
    reauthenticate()
    throw new ApiError(401, 'セッションの有効期限が切れました。再ログインします…')
  }
  if (!res.ok) {
    const body: unknown = await res.json().catch(() => null)
    const detail = (body as { detail?: unknown } | null)?.detail
    throw new ApiError(res.status, typeof detail === 'string' ? detail : `HTTP ${res.status}`)
  }
  return (await res.json()) as T
}

const json = (body: unknown): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

export const listAssets = (user: User, limit = 50, offset = 0) =>
  request<{ assets: VideoAsset[] }>(user, `/api/video/assets?limit=${limit}&offset=${offset}`)

export const getAsset = (user: User, id: string) =>
  request<VideoAssetDetail>(user, `/api/video/assets/${encodeURIComponent(id)}`)

export const getPlayback = (user: User, id: string) =>
  request<{ url: string; expires_at: string }>(
    user, `/api/video/assets/${encodeURIComponent(id)}/playback`,
  )

/** 1 リクエスト 1 本(specs/20 §2)。複数件は**画面が順に投げる**。 */
export function uploadAsset(
  user: User, file: File, meta: Record<string, string>,
): Promise<VideoAsset> {
  const form = new FormData()
  form.append('file', file)
  for (const [key, value] of Object.entries(meta)) {
    if (value.trim()) form.append(key, value.trim())
  }
  return request<VideoAsset>(user, '/api/video/assets', { method: 'POST', body: form })
}

export const analyzeAsset = (user: User, id: string) =>
  request<{ analysis_state: string; analysis_error: string | null; scene_count: number }>(
    user, `/api/video/assets/${encodeURIComponent(id)}/analyze`, { method: 'POST' },
  )

export const deleteAsset = (user: User, id: string) =>
  request<{ deleted: boolean }>(
    user, `/api/video/assets/${encodeURIComponent(id)}`, { method: 'DELETE' },
  )

export const searchScenes = (user: User, body: SearchBody) =>
  request<SearchResult>(user, '/api/video/search', json(body))

export const patchScene = (user: User, id: string, changes: Record<string, unknown>) =>
  request<VideoScene & { embedding_state: string; embedding_error: string | null }>(
    user, `/api/video/scenes/${encodeURIComponent(id)}`,
    { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(changes) },
  )

export const confirmScene = (user: User, id: string) =>
  request<VideoScene>(
    user, `/api/video/scenes/${encodeURIComponent(id)}/confirm`, { method: 'POST' },
  )

export const listSceneEdits = (user: User, id: string) =>
  request<{ edits: SceneEdit[] }>(user, `/api/video/scenes/${encodeURIComponent(id)}/edits`)

export const deleteScene = (user: User, id: string) =>
  request<{ deleted: boolean }>(
    user, `/api/video/scenes/${encodeURIComponent(id)}`, { method: 'DELETE' },
  )

/** 例外 → 画面に出す 1 行。**理由をそのまま見せる**(API の detail は利用者向けの文)。 */
export const errorText = (e: unknown): string =>
  e instanceof ApiError ? e.message : String(e instanceof Error ? e.message : e)
