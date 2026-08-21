import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom'
import { AuthProvider } from '../../auth'
import { PrefsProvider } from '../../prefs'
import VideoDetailRoute from './detail'
import type { VideoAssetDetail } from './types'

/** 詳細画面は URL(`?t=` / `?scene=`)が指す場面を頭出しする。**同じ画面を使い回さない**
 *  ことをここで固定する —— 「検索結果 → 詳細 → 別の結果（同じ映像の別の場面）」で
 *  2 回目の頭出しが効かないと、この機能の主要な価値が静かに壊れる。 */

const asset: VideoAssetDetail = {
  id: 'a1', title: '現場の記録', created_at: '2026-08-19T22:00:00Z', captured_at: null,
  duration_ms: 15_200, collection: 'E2E', category: null, rights: null,
  analysis_state: 'done', analysis_error: null, vision_state: 'skipped',
  thumb_object: null, summary: null,
  scenes: [
    {
      id: 's1', start_ms: 0, end_ms: 5000, description: '青い背景にテロップ',
      tags: '["雨"]', objects: '[]', people: null, actions: '[]',
      place: 'unknown', scene_kind: 'unknown', indoor: 'unknown',
      time_of_day: 'unknown', weather: '雨', screen_text: null,
      thumb_object: null, source: 'ai', confirmed_at: null,
    },
    {
      id: 's2', start_ms: 10_200, end_ms: 15_200, description: '緑の背景に点検完了',
      tags: '[]', objects: '[]', people: null, actions: '[]',
      place: 'unknown', scene_kind: '建物内', indoor: 'unknown',
      time_of_day: 'unknown', weather: 'unknown', screen_text: null,
      thumb_object: null, source: 'human', confirmed_at: '2026-08-20T08:44:00Z',
    },
  ],
}

let playbackUrls = ['https://par.example/v1']

vi.mock('./api', () => ({
  getAsset: vi.fn(() => Promise.resolve(asset)),
  getPlayback: vi.fn(() =>
    Promise.resolve({ url: playbackUrls.shift() ?? 'https://par.example/last', expires_at: '' }),
  ),
  analyzeAsset: vi.fn(), confirmScene: vi.fn(), deleteScene: vi.fn(),
  listSceneEdits: vi.fn(() => Promise.resolve({ edits: [] })), patchScene: vi.fn(),
  errorText: (e: unknown) => String(e),
}))

/** 画面内から**実際に遷移する**ための小さな部品(検索結果を続けて選ぶ操作に相当)。 */
function Go({ to }: { to: string }) {
  const navigate = useNavigate()
  return <button onClick={() => navigate(to)}>go</button>
}

const renderAt = (url: string, next?: string) =>
  render(
    <PrefsProvider>
      <AuthProvider>
        <MemoryRouter initialEntries={[url]}>
          {next && <Go to={next} />}
          <Routes>
            <Route path="/videos/:id" element={<VideoDetailRoute />} />
          </Routes>
        </MemoryRouter>
      </AuthProvider>
    </PrefsProvider>,
  )

const player = () => screen.getByTestId('video-player') as HTMLVideoElement

beforeEach(() => {
  vi.clearAllMocks()
  playbackUrls = ['https://par.example/v1']
  // jsdom は play() を実装していない(呼ぶと「Not implemented」を出して undefined を返す)。
  // 画面は自動再生を試みるので、ここで**ブラウザと同じ形**(Promise)に寄せる
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
})

describe('VideoDetail', () => {
  it('?t= の秒から再生位置を合わせる', async () => {
    renderAt('/videos/a1?t=10.2&scene=s2')
    await waitFor(() => expect(screen.queryByTestId('video-player')).not.toBeNull())
    player().dispatchEvent(new Event('loadedmetadata'))
    expect(player().currentTime).toBeCloseTo(10.2)
    expect(screen.getByText(/検索結果の場面から再生/)).toBeInTheDocument()
  })

  it('同じ映像の別の場面へ移ると頭出しをやり直す', async () => {
    // 検索結果 → 詳細 → 別の結果（同じ映像の別の場面）。ルータは同じ画面を使い回すので、
    // 作り直さないと 2 回目の `?t=` が当たらない
    renderAt('/videos/a1?t=0&scene=s1', '/videos/a1?t=10.2&scene=s2')
    await waitFor(() => expect(screen.queryByTestId('video-player')).not.toBeNull())
    player().dispatchEvent(new Event('loadedmetadata'))
    expect(player().currentTime).toBe(0)

    screen.getByText('go').click()
    // 作り直しでは再生 URL を取り直すので、**プレーヤが出るまで**待つ
    await waitFor(() => {
      expect(screen.getByText(/検索結果の場面から再生/).textContent).toContain('0:10')
      expect(screen.queryByTestId('video-player')).not.toBeNull()
    })
    player().dispatchEvent(new Event('loadedmetadata'))
    expect(player().currentTime).toBeCloseTo(10.2)
  })

  it('読めない ?t= は先頭から再生し、そのことを画面に出す', async () => {
    renderAt('/videos/a1?t=abc')
    await waitFor(() => expect(screen.queryByTestId('video-player')).not.toBeNull())
    player().dispatchEvent(new Event('loadedmetadata'))
    expect(player().currentTime).toBe(0)
    expect(screen.getByText(/先頭から再生します/)).toBeInTheDocument()
    expect(screen.queryByText(/検索結果の場面から再生/)).not.toBeInTheDocument()
  })

  it('再生 URL を取り直して src が変わっても頭出しし直す', async () => {
    // 再生 URL は期限付き PAR。取り直すとメディアが読み直されて位置が 0 に戻るので、
    // **新しい src には当て直す**（実ブラウザで踏んだ回帰）
    playbackUrls = ['https://par.example/v1', 'https://par.example/v2']
    renderAt('/videos/a1?t=10.2&scene=s2')
    await waitFor(() => expect(screen.queryByTestId('video-player')).not.toBeNull())
    player().dispatchEvent(new Event('loadedmetadata'))
    expect(player().currentTime).toBeCloseTo(10.2)

    // 2 本目の PAR が届いて src が変わる = ブラウザは位置を捨てて読み直す
    Object.defineProperty(player(), 'currentSrc', {
      value: 'https://par.example/v2', configurable: true,
    })
    player().currentTime = 0
    player().dispatchEvent(new Event('loadedmetadata'))
    expect(player().currentTime).toBeCloseTo(10.2)
  })

  it('同じ src では当て直さない(利用者が選んだ場面を上書きしない)', async () => {
    renderAt('/videos/a1?t=10.2&scene=s2')
    await waitFor(() => expect(screen.queryByTestId('video-player')).not.toBeNull())
    player().dispatchEvent(new Event('loadedmetadata'))
    expect(player().currentTime).toBeCloseTo(10.2)

    // 利用者がタイムラインで別の場面へ移った状態を作り、同じ src で再度イベントが来ても
    // `?t=` へ引き戻さない
    screen.getAllByTestId('scene-row')[0].click()
    expect(player().currentTime).toBe(0)
    player().dispatchEvent(new Event('loadedmetadata'))
    expect(player().currentTime).toBe(0)
  })

  it('自動再生が拒まれても再生位置は合っている', async () => {
    // ブラウザの自動再生方針で play() が拒まれることがある。**位置合わせは先に済ませる**
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockRejectedValue(
      new DOMException('NotAllowedError'),
    )
    renderAt('/videos/a1?t=10.2&scene=s2')
    await waitFor(() => expect(screen.queryByTestId('video-player')).not.toBeNull())
    player().dispatchEvent(new Event('loadedmetadata'))
    expect(player().currentTime).toBeCloseTo(10.2)
  })

  it('場面の一覧と出所を出す(誰の言葉かを消さない)', async () => {
    renderAt('/videos/a1')
    await waitFor(() => expect(screen.getAllByTestId('scene-row')).toHaveLength(2))
    expect(screen.getByText('AI が付与')).toBeInTheDocument()
  })
})
