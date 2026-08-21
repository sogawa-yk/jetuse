/** 映像の場面検索に「?」（構成図ポップアップ）が付いていることを固定する。
 *
 *  他のユースケースは全て「?」でバックエンドの構成が読める。映像検索だけ無いと、
 *  利用者から見て**同じ機能があるのに一箇所だけ欠けている**状態になる（2026-08-21 指摘）。
 *
 *  画面の配線（helpKey）はテストから見えにくく、ページを書き換えたときに黙って外れる。
 *  ソースを読んで確かめる。 */
import { readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

import { HELP_CONTENT } from '../../components/helpContent'

const here = join(import.meta.dirname ?? __dirname)

describe('映像の場面検索のヘルプ', () => {
  it('helpContent に videos があり、図の実体を指している', () => {
    const e = HELP_CONTENT.videos
    expect(e).toBeTruthy()
    expect(e.diagram).toMatch(/^\/architecture\/usecase-videos\.png$/)
  })

  it('図の正本（docs/architecture/usecases）に実体がある', () => {
    // public/architecture は生成物なので、**正本側**を見る（copy-architecture.mjs がコピーする）。
    const root = join(here, '..', '..', '..', '..', '..')
    const name = HELP_CONTENT.videos.diagram.replace('/architecture/', '')
    const p = join(root, 'docs', 'architecture', 'usecases', name)
    // png はバイナリなので存在とサイズだけ見る
    expect(statSync(p).size).toBeGreaterThan(10_000)
  })

  it('3画面すべてに helpKey="videos" が付いている', () => {
    // 一覧・検索・詳細のどれから入っても構成が読めること。
    for (const f of [
      join(here, '..', 'videos.tsx'),
      join(here, 'search.tsx'),
      join(here, 'detail.tsx'),
    ]) {
      const src = readFileSync(f, 'utf-8')
      expect(src, `${f} に helpKey が無い`).toContain('helpKey="videos"')
    }
  })
})
