import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { PrefsProvider } from '../../prefs'
import { Timeline } from './Timeline'

/** タイムラインの帯は「選ぶとその場面へ移動する」ためにある(要求7)。
 *  押せること・押した場面が返ること・種別で色が分かれることを固定する。 */

const scenes = [
  { id: 's1', start_ms: 0, end_ms: 30_000, scene_kind: 'スタジオ' },
  { id: 's2', start_ms: 30_000, end_ms: 60_000, scene_kind: '屋外' },
]

const renderTimeline = (onPick = vi.fn(), selectedId: string | null = null) => {
  render(
    <PrefsProvider>
      <Timeline
        scenes={scenes}
        durationMs={60_000}
        currentMs={0}
        selectedId={selectedId}
        onPick={onPick}
      />
    </PrefsProvider>,
  )
  return onPick
}

describe('Timeline', () => {
  it('場面ごとに押せる帯を出す', () => {
    renderTimeline()
    expect(screen.getAllByTestId('timeline-band')).toHaveLength(2)
  })

  it('帯を選ぶとその場面が返る(= その時刻へ移動する)', () => {
    const onPick = renderTimeline()
    fireEvent.click(screen.getAllByTestId('timeline-band')[1])
    expect(onPick).toHaveBeenCalledWith(scenes[1])
  })

  it('種別で色を分ける', () => {
    renderTimeline()
    const [a, b] = screen.getAllByTestId('timeline-band')
    expect(a.style.backgroundColor).not.toBe('')
    expect(a.style.backgroundColor).not.toBe(b.style.backgroundColor)
  })

  it('選択中の帯が判る', () => {
    renderTimeline(vi.fn(), 's2')
    const [a, b] = screen.getAllByTestId('timeline-band')
    expect(a.getAttribute('aria-pressed')).toBe('false')
    expect(b.getAttribute('aria-pressed')).toBe('true')
  })

  it('場面が無ければ「分析すると作られる」と伝える(空の帯を出さない)', () => {
    render(
      <PrefsProvider>
        <Timeline scenes={[]} durationMs={null} currentMs={0} selectedId={null} onPick={vi.fn()} />
      </PrefsProvider>,
    )
    expect(screen.queryAllByTestId('timeline-band')).toHaveLength(0)
    expect(screen.getByText(/分析/)).toBeInTheDocument()
  })
})
