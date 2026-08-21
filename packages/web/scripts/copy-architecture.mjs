/** アーキ図(docs/architecture/usecases/*.png / *.svg)を public/architecture/ へ複製する。
 *  ヘルプポップアップ(feedback 20260620 #4)がSPAから配信して表示するため、build/dev前に実行する。
 *  public/architecture/ は生成物のため .gitignore 済み(正本は docs 側)。 */
import { cpSync, mkdirSync, readdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const src = join(here, '..', '..', '..', 'docs', 'architecture', 'usecases')
const dest = join(here, '..', 'public', 'architecture')

try {
  mkdirSync(dest, { recursive: true })
  // **svg も対象にする。** 図の正本は .drawio で、プレビューは png（drawio から書き出す）だが、
  // 書き出しツールが無い環境で作った図は svg を直接置く（<img> はどちらも表示できる）。
  const figs = readdirSync(src).filter((f) => f.endsWith('.png') || f.endsWith('.svg'))
  for (const f of figs) cpSync(join(src, f), join(dest, f))
  console.log(`[copy-architecture] copied ${figs.length} figure(s) -> public/architecture/`)
} catch (e) {
  // 図が無くてもビルド自体は通す(ヘルプ画像が出ないだけ)
  console.warn(`[copy-architecture] skipped: ${e.message}`)
}
