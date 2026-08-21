import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { PrefsProvider } from '../../prefs'
import { SceneCard } from './SceneCard'
import type { SearchHit } from './types'

/** 場面カードが守る 2 つの約束(tasks/VID-06 の禁止事項をそのままテストにする):
 *   1. **一致理由を出す**(要求11。AI 検索をブラックボックスにしない)
 *   2. **その場面へ移動できる**(要求6。`?t=<秒>` 付きのリンク) */

const hit = (over: Partial<SearchHit> = {}): SearchHit => ({
  scene_id: 's1', asset_id: 'a1', title: '現場の記録',
  start_ms: 73_000, end_ms: 91_000,
  thumb_url: 'https://par.example/thumb.jpg',
  description: '傘を差した人物が濡れた路面の前で話している',
  tags: ['雨', '屋外'], objects: ['傘'], actions: ['話している'],
  people: { present: 'yes', count: 1 },
  place: 'unknown', scene_kind: '屋外', indoor: 'outdoor',
  time_of_day: 'night', weather: '雨', screen_text: null,
  source: 'ai', confirmed_at: null,
  matched: {
    reason: '「豪雨」に意味が近い場面です(距離 0.501・42 件中 1 位)',
    fields: ['tags', 'weather'], tags: ['雨'], distance: 0.501,
  },
  asset: {
    collection: '設備点検', category: '点検', rights: '社内限定',
    captured_at: '2026-08-19T10:00:00Z', created_at: '2026-08-19T22:00:00Z',
    duration_ms: 120_000, analysis_state: 'done',
  },
  ...over,
})

const renderCard = (h: SearchHit) =>
  render(
    <PrefsProvider>
      <MemoryRouter>
        <SceneCard hit={h} />
      </MemoryRouter>
    </PrefsProvider>,
  )

describe('SceneCard', () => {
  it('一致理由を必ず画面に出す(要求11)', () => {
    renderCard(hit())
    expect(screen.getByTestId('scene-reason')).toHaveTextContent(
      '「豪雨」に意味が近い場面です(距離 0.501・42 件中 1 位)',
    )
    // 距離は理由文とチップの両方に出る(どちらも根拠の一部)
    expect(screen.getAllByText(/距離 0\.501/).length).toBeGreaterThan(0)
  })

  it('効いた項目は画面のラベルで出す(列名をそのまま見せない)', () => {
    renderCard(hit())
    expect(screen.getByText('タグ')).toBeInTheDocument()
    expect(screen.getByText('天候')).toBeInTheDocument()
  })

  it('その場面の時刻へ移動できるリンクになっている(要求6)', () => {
    renderCard(hit())
    for (const link of screen.getAllByRole('link')) {
      expect(link.getAttribute('href')).toBe('/videos/a1?t=73&scene=s1')
    }
  })

  it('場面の開始と終了・映像の基本情報を出す', () => {
    renderCard(hit())
    expect(screen.getByText('1:13 – 1:31')).toBeInTheDocument()
    expect(screen.getByText(/設備点検/)).toHaveTextContent('2026-08-19 10:00')
  })

  it('サムネイルが無くても理由と移動先は消えない', () => {
    renderCard(hit({ thumb_url: null }))
    expect(screen.getByTestId('scene-reason')).toHaveTextContent('豪雨')
    expect(screen.getAllByRole('link')[0].getAttribute('href')).toContain('?t=73')
  })

  it('条件だけの検索(距離なし)でも理由は出す', () => {
    renderCard(hit({
      matched: {
        reason: '条件(屋内外=屋外)に一致しています', fields: ['indoor'], tags: [],
        distance: null,
      },
    }))
    expect(screen.getByTestId('scene-reason')).toHaveTextContent('条件(屋内外=屋外)に一致しています')
    expect(screen.queryByText(/距離/)).not.toBeInTheDocument()
  })
})
