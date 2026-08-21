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

/** 1 リクエスト 1 本(specs/20 §2)。複数件は**画面が順に投げる**。
 *
 *  **この経路は小さい映像専用**。本文が API Gateway を通るので 20 MiB(実測)が天井で、
 *  それを超えるとアプリに届く前に 413 になる。画面は `uploadAssetDirect` を使う。
 */
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

/** 直接アップロードの引換券(VID-07)。`upload_url` は**書き込み専用・短命**の PAR。 */
export type UploadTicket = {
  id: string
  upload_url: string
  object_name: string
  content_type: string
  expires_at: string
  max_bytes: number
}

export const createUploadUrl = (user: User, body: Record<string, unknown>) =>
  request<UploadTicket>(user, '/api/video/assets/upload-url', json(body))

export const completeUpload = (user: User, id: string) =>
  request<VideoAsset>(
    user, `/api/video/assets/${encodeURIComponent(id)}/complete`, { method: 'POST' },
  )

/** Object Storage へ本体を直接 PUT する。
 *
 *  **XHR を使う。** `fetch` は上りの進捗を出せない(`ReadableStream` の要求本文は
 *  対応が限られる)。100MB 級を上げる画面で進捗が出ないと、利用者は固まったのか
 *  進んでいるのか判らない。
 *
 *  `Content-Type` は**発行時に貰った値をそのまま**付ける。サーバは complete で同じ
 *  規則から計算し直した値と突き合わせるので、ここで別の型を付けると弾かれる。
 */
export function putToStorage(
  ticket: UploadTicket, file: File, onProgress: (ratio: number) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('PUT', ticket.upload_url)
    xhr.setRequestHeader('Content-Type', ticket.content_type)
    xhr.upload.onprogress = (e: ProgressEvent) => {
      if (e.lengthComputable && e.total > 0) onProgress(e.loaded / e.total)
    }
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress(1)
        resolve()
        return
      }
      // **理由を落とさない。** URL が効かない(期限切れ / 確定済みで消した)のと、
      // 通信そのものの失敗を「失敗しました」に丸めない。実測では、確定後に消した
      // PAR へもう一度 PUT すると **401**、期限切れは 404 が返る
      reject(new ApiError(
        xhr.status,
        [401, 403, 404].includes(xhr.status)
          ? 'アップロード先の URL が使えません(期限切れか、すでに登録が確定しています)。もう一度登録してください'
          : `アップロードに失敗しました (HTTP ${xhr.status})`,
      ))
    }
    xhr.onerror = () => reject(new ApiError(0, 'アップロードの通信が切れました'))
    xhr.onabort = () => reject(new ApiError(0, 'アップロードを中止しました'))
    xhr.send(file)
  })
}

/** 2 段 + 直接 PUT の登録(VID-07)。**本体はゲートウェイを通らない**。
 *
 *  途中で失敗しても台帳には `uploading` の行が残るだけで、確定はしない。
 *  放置された行はサーバ側の回収(`reap_stale_uploads`)が引き取る。
 */
export async function uploadAssetDirect(
  user: User, file: File, meta: Record<string, string>,
  onProgress: (ratio: number) => void,
): Promise<VideoAsset> {
  const body: Record<string, unknown> = { filename: file.name, size_bytes: file.size }
  for (const [key, value] of Object.entries(meta)) {
    if (value.trim()) body[key] = value.trim()
  }
  const ticket = await createUploadUrl(user, body)
  onProgress(0)
  await putToStorage(ticket, file, onProgress)
  return completeUpload(user, ticket.id)
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
