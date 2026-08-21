import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { ApiError, uploadAssetDirect } from './api'
import type { User } from '../../auth'

/** VID-07 直接アップロード。**本体をゲートウェイに通さない**経路の画面側。
 *
 *  ここで固定するのは 3 つ。
 *  1. 本体は API ではなく **貰った PAR の URL へ** PUT すること（通してしまうと 20 MiB で 413）
 *  2. PUT の `Content-Type` は**発行時に貰った値そのまま**（サーバが complete で突き合わせる）
 *  3. PUT に失敗したら **complete を呼ばない**（入っていないものを確定させない）
 */

const user = { token: 't', name: 'dev' } as unknown as User

const TICKET = {
  id: 'asset-1',
  upload_url: 'https://objectstorage.example.com/p/token/n/ns/b/bkt/o/video/dev/asset-1/source.mp4',
  object_name: 'video/dev/asset-1/source.mp4',
  content_type: 'video/mp4',
  expires_at: '2026-08-20T15:00:00+00:00',
  max_bytes: 524_288_000,
}

type Sent = { url: string; headers: Record<string, string>; body: unknown }

/** 送った PUT を記録する XHR。進捗イベントも 1 回流す。 */
function installXhr(status = 200): Sent[] {
  const sent: Sent[] = []
  class FakeXhr {
    status = 0
    upload: { onprogress?: (e: ProgressEvent) => void } = {}
    onload?: () => void
    onerror?: () => void
    onabort?: () => void
    private url = ''
    private headers: Record<string, string> = {}

    open(_method: string, url: string) {
      this.url = url
    }

    setRequestHeader(key: string, value: string) {
      this.headers[key] = value
    }

    send(body: unknown) {
      sent.push({ url: this.url, headers: this.headers, body })
      this.upload.onprogress?.(
        { lengthComputable: true, loaded: 50, total: 100 } as ProgressEvent,
      )
      this.status = status
      this.onload?.()
    }
  }
  vi.stubGlobal('XMLHttpRequest', FakeXhr)
  return sent
}

const jsonResponse = (body: unknown) =>
  ({ ok: true, status: 200, json: () => Promise.resolve(body) }) as Response

const file = (name = 'big.mp4', size = 150 * 1024 * 1024) =>
  ({ name, size, type: 'video/mp4' }) as File

beforeEach(() => {
  vi.restoreAllMocks()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('uploadAssetDirect', () => {
  it('本体は API ではなく PAR へ PUT し、メタデータだけを API へ送る', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(TICKET))
      .mockResolvedValueOnce(jsonResponse({ id: 'asset-1', bytes: 157_286_400 }))
    vi.stubGlobal('fetch', fetchMock)
    const sent = installXhr()
    const seen: number[] = []

    const target = file()
    const asset = await uploadAssetDirect(
      user, target, { title: '4K の記録', collection: '  ', category: '' },
      (r) => seen.push(r),
    )

    // 1 段目: 引換券。**本体は載らない**（申告だけ）
    const [urlOne, initOne] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(urlOne).toBe('/api/video/assets/upload-url')
    expect(JSON.parse(initOne.body as string)).toEqual({
      filename: 'big.mp4', size_bytes: 150 * 1024 * 1024, title: '4K の記録',
    })

    // 2 段目: 本体は Object Storage へ直接。Content-Type は貰った値そのまま
    expect(sent).toHaveLength(1)
    expect(sent[0].url).toBe(TICKET.upload_url)
    expect(sent[0].headers['Content-Type']).toBe('video/mp4')
    // File をそのまま送る（画面側でメモリに読み込まない）
    expect(sent[0].body).toBe(target)

    // 3 段目: 確定。**API を叩くのは 2 回だけ**（本体はゲートウェイを通らない）
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(fetchMock.mock.calls[1][0]).toBe('/api/video/assets/asset-1/complete')
    expect(asset.id).toBe('asset-1')
    // 進捗は 0 → 途中 → 1 まで出る（固まったのか進んでいるのかが判る）
    expect(seen[0]).toBe(0)
    expect(seen).toContain(0.5)
    expect(seen[seen.length - 1]).toBe(1)
  })

  it('PUT に失敗したら complete を呼ばない（入っていないものを確定させない）', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse(TICKET))
    vi.stubGlobal('fetch', fetchMock)
    installXhr(403)

    await expect(uploadAssetDirect(user, file(), {}, () => {})).rejects.toBeInstanceOf(ApiError)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it.each([401, 403, 404])('URL が効かないとき(%i)は理由が判る文言で返す', async (status) => {
    // 実測: 確定後に消した PAR へもう一度 PUT すると 401、期限切れは 404
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(jsonResponse(TICKET)))
    installXhr(status)

    await expect(uploadAssetDirect(user, file(), {}, () => {}))
      .rejects.toThrow(/期限切れか、すでに登録が確定/)
  })

  it('API が 413 を返したら、その理由をそのまま渡す（丸めない）', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce({
      ok: false, status: 413,
      json: () => Promise.resolve({ detail: '映像が大きすぎます(上限 500MB)' }),
    } as Response))
    installXhr()

    await expect(uploadAssetDirect(user, file(), {}, () => {}))
      .rejects.toThrow('映像が大きすぎます(上限 500MB)')
  })
})
