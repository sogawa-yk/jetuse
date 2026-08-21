import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { AuthProvider } from '../../auth'
import { PrefsProvider } from '../../prefs'
import VideoSearch from './search'
import * as api from './api'
import type { SearchResult } from './types'

/** 検索ページは**検索語を URL に持つ**（結果カード → 詳細 → 戻る、で同じ結果に戻れるように）。
 *  URL から検索し直す経路が、検索語をそのまま渡すことをここで固定する。 */

const result = (q: string): SearchResult => ({
  mode: 'vector', total: 1, returned: 1, excluded_no_vector: 0,
  hits: [{
    scene_id: 's1', asset_id: 'a1', title: '現場の記録',
    start_ms: 5000, end_ms: 10_000, thumb_url: null, description: `${q} の場面`,
    tags: [], objects: [], actions: [], people: null,
    place: null, scene_kind: null, indoor: null, time_of_day: null, weather: null,
    screen_text: null, source: 'ai', confirmed_at: null,
    matched: { reason: `「${q}」に意味が近い場面です`, fields: [], tags: [], distance: 0.3 },
    asset: {
      collection: null, category: null, rights: null, captured_at: null,
      created_at: null, duration_ms: 15_200, analysis_state: 'done',
    },
  }],
})

vi.mock('./api', async () => ({
  searchScenes: vi.fn(),
  errorText: (e: unknown) => String(e),
}))

const searchScenes = vi.mocked(api.searchScenes)

const renderAt = (url: string) =>
  render(
    <PrefsProvider>
      <AuthProvider>
        <MemoryRouter initialEntries={[url]}>
          <Routes>
            <Route path="/videos/search" element={<VideoSearch />} />
          </Routes>
        </MemoryRouter>
      </AuthProvider>
    </PrefsProvider>,
  )

beforeEach(() => {
  vi.clearAllMocks()
  searchScenes.mockImplementation((_u, body) =>
    Promise.resolve(result(String(body.q ?? body.similar_to_scene_id ?? ''))),
  )
})

describe('VideoSearch', () => {
  it('`?q=` が付いていれば着いた時点で検索して結果を出す(戻ったときの復元)', async () => {
    renderAt('/videos/search?q=' + encodeURIComponent('豪雨'))
    await waitFor(() => expect(screen.getByTestId('search-total')).toBeInTheDocument())
    expect(searchScenes.mock.calls[0][1].q).toBe('豪雨')
    expect(screen.getByTestId('scene-reason')).toHaveTextContent('「豪雨」に意味が近い')
  })

  it('検索語に `|` が入っても切り落とさない', async () => {
    renderAt('/videos/search?q=' + encodeURIComponent('設備|点検'))
    await waitFor(() => expect(searchScenes).toHaveBeenCalled())
    expect(searchScenes.mock.calls[0][1].q).toBe('設備|点検')
  })

  it('`?similar=` は類似検索として実行する(検索語は載せない)', async () => {
    renderAt('/videos/search?similar=s9')
    await waitFor(() => expect(searchScenes).toHaveBeenCalled())
    const body = searchScenes.mock.calls[0][1]
    expect(body.similar_to_scene_id).toBe('s9')
    expect(body.q).toBeUndefined()
    expect(screen.getByText(/この場面に似た場面を探しています/)).toBeInTheDocument()
  })

  it('条件も検索語も無ければ勝手に検索しない', async () => {
    renderAt('/videos/search')
    await waitFor(() => expect(screen.getByTestId('search-q')).toBeInTheDocument())
    expect(searchScenes).not.toHaveBeenCalled()
  })
})
