import { describe, expect, it } from 'vitest'
import {
  asObject, asStringList, buildSearchBody, EMPTY_SEARCH_FORM, formatRange, formatTimecode,
  formatUtc, hasAnyCondition, parseSeekSeconds, sceneBands, sceneKindColor,
  scenePlayHref, SearchFormError, splitTags, toSeekSeconds,
} from './format'

describe('時刻', () => {
  it('分:秒。1 時間以上は 時:分:秒', () => {
    expect(formatTimecode(0)).toBe('0:00')
    expect(formatTimecode(73_000)).toBe('1:13')
    expect(formatTimecode(3_723_000)).toBe('1:02:03')
  })

  it('壊れた値でも落ちない(0:00 に寄せる)', () => {
    expect(formatTimecode(null)).toBe('0:00')
    expect(formatTimecode(-5000)).toBe('0:00')
    expect(formatTimecode(Number.NaN)).toBe('0:00')
  })

  it('場面は開始と終了を両方出す', () => {
    expect(formatRange(73_000, 91_000)).toBe('1:13 – 1:31')
  })
})

describe('?t= の受け渡し', () => {
  it('場面カードのリンクは秒で頭出しを載せる', () => {
    expect(scenePlayHref('a1', 73_000, 's1')).toBe('/videos/a1?t=73&scene=s1')
    expect(scenePlayHref('a1', 73_500)).toBe('/videos/a1?t=73.5')
  })

  it('id は URL エンコードする', () => {
    expect(scenePlayHref('a/1', 0)).toBe('/videos/a%2F1?t=0')
  })

  it('読めない ?t= は「指定なし」。0 秒に丸めない', () => {
    expect(parseSeekSeconds('73.5')).toBe(73.5)
    expect(parseSeekSeconds('0')).toBe(0)
    expect(parseSeekSeconds(null)).toBeNull()
    expect(parseSeekSeconds('')).toBeNull()
    expect(parseSeekSeconds('abc')).toBeNull()
    expect(parseSeekSeconds('-3')).toBeNull()
  })

  it('秒は小数第 1 位まで(余計な 0 を付けない)', () => {
    expect(toSeekSeconds(73_000)).toBe('73')
    expect(toSeekSeconds(73_460)).toBe('73.5')
  })
})

describe('タイムラインの帯', () => {
  const scenes = [
    { id: 's1', start_ms: 0, end_ms: 30_000, scene_kind: 'スタジオ' },
    { id: 's2', start_ms: 30_000, end_ms: 60_000, scene_kind: '屋外' },
  ]

  it('尺に対する位置と幅(%)を返す', () => {
    const bands = sceneBands(scenes, 60_000)
    expect(bands[0].leftPct).toBe(0)
    expect(bands[0].widthPct).toBe(50)
    expect(bands[1].leftPct).toBe(50)
    expect(bands[1].widthPct).toBe(50)
  })

  it('尺が未取得(NULL)でも場面の終端から組み立てる', () => {
    const bands = sceneBands(scenes, null)
    expect(bands).toHaveLength(2)
    expect(bands[1].leftPct).toBe(50)
  })

  it('尺が場面より短くても帯をはみ出させない', () => {
    const bands = sceneBands(scenes, 10_000)
    for (const b of bands) expect(b.leftPct + b.widthPct).toBeLessThanOrEqual(100.001)
  })

  it('短すぎる場面も押せる幅を残す(押せない帯はその場面へ移動できない)', () => {
    const [band] = sceneBands([{ id: 's', start_ms: 0, end_ms: 100 }], 3_600_000)
    expect(band.widthPct).toBeGreaterThanOrEqual(0.6)
  })

  it('**末尾**の短い場面でも最小幅を保ち、右端からはみ出さない', () => {
    const [band] = sceneBands(
      [{ id: 's', start_ms: 3_599_900, end_ms: 3_600_000 }], 3_600_000,
    )
    expect(band.widthPct).toBeGreaterThanOrEqual(0.6)
    expect(band.leftPct + band.widthPct).toBeLessThanOrEqual(100.001)
  })

  it('尺も場面も無ければ帯は無い', () => {
    expect(sceneBands([], null)).toEqual([])
  })

  it('同じ種別は同じ色・不明は中立色', () => {
    expect(sceneKindColor('屋外')).toBe(sceneKindColor('屋外'))
    expect(sceneKindColor('屋外')).not.toBe(sceneKindColor('スタジオ'))
    expect(sceneKindColor('unknown')).toBe(sceneKindColor(null))
  })
})

describe('検索条件の組み立て', () => {
  it('未入力の欄は送らない(条件なしなら filters ごと無い)', () => {
    expect(buildSearchBody(EMPTY_SEARCH_FORM)).toEqual({ limit: 20 })
  })

  it('入力した条件だけを載せる', () => {
    const body = buildSearchBody({
      ...EMPTY_SEARCH_FORM, q: ' 豪雨 ', collection: '設備点検',
      indoor: 'outdoor', has_people: 'true', confirmed: 'false',
      tags: '雨、屋外 夜', duration_max_sec: '600', limit: 5,
    })
    expect(body).toEqual({
      q: '豪雨',
      limit: 5,
      filters: {
        collection: '設備点検', indoor: 'outdoor', has_people: true,
        confirmed: false, tags: ['雨', '屋外', '夜'], duration_max_ms: 600_000,
      },
    })
  })

  it('尺は秒で入れてミリ秒で送る。読めない値は落とさず拒む', () => {
    expect(
      buildSearchBody({ ...EMPTY_SEARCH_FORM, duration_min_sec: '1.5' }).filters,
    ).toEqual({ duration_min_ms: 1500 })
    expect(() =>
      buildSearchBody({ ...EMPTY_SEARCH_FORM, duration_max_sec: 'すぐ' }),
    ).toThrow(SearchFormError)
    expect(() =>
      buildSearchBody({ ...EMPTY_SEARCH_FORM, duration_min_sec: '-1' }),
    ).toThrow(SearchFormError)
  })

  it('タグの上限を超えたら送らずに拒む(API の 422 を待たない)', () => {
    const many = Array.from({ length: 11 }, (_, i) => `t${i}`).join(',')
    expect(() => buildSearchBody({ ...EMPTY_SEARCH_FORM, tags: many })).toThrow(
      SearchFormError,
    )
  })

  it('類似検索を指定したら検索語は載せない(API は同時指定を 422)', () => {
    const body = buildSearchBody(
      { ...EMPTY_SEARCH_FORM, q: '豪雨' }, { similarToSceneId: 's1' },
    )
    expect(body.similar_to_scene_id).toBe('s1')
    expect(body.q).toBeUndefined()
  })

  it('条件の有無を判定できる', () => {
    expect(hasAnyCondition(EMPTY_SEARCH_FORM)).toBe(false)
    expect(hasAnyCondition({ ...EMPTY_SEARCH_FORM, q: '雨' })).toBe(true)
    expect(hasAnyCondition({ ...EMPTY_SEARCH_FORM, indoor: 'outdoor' })).toBe(true)
  })

  it('タグは読点・カンマ・空白で切る', () => {
    expect(splitTags(' 雨、屋外, 夜 ')).toEqual(['雨', '屋外', '夜'])
    expect(splitTags('   ')).toEqual([])
  })
})

describe('台帳の JSON 列(詳細 API は文字列のまま返す)', () => {
  it('文字列でも配列でも読める。壊れていれば空', () => {
    expect(asStringList('["雨","屋外"]')).toEqual(['雨', '屋外'])
    expect(asStringList(['雨'])).toEqual(['雨'])
    expect(asStringList('{壊れた')).toEqual([])
    expect(asStringList(null)).toEqual([])
    expect(asStringList('[1,"雨"]')).toEqual(['雨'])
  })

  it('オブジェクト列も同じ扱い', () => {
    expect(asObject('{"present":"yes","count":1}')).toEqual({ present: 'yes', count: 1 })
    expect(asObject('[1]')).toBeNull()
    expect(asObject('{壊れた')).toBeNull()
  })
})

describe('日時', () => {
  it('UTC のまま分まで出す', () => {
    expect(formatUtc('2026-08-19T10:00:00Z')).toBe('2026-08-19 10:00')
    expect(formatUtc(null)).toBe('')
  })
})
