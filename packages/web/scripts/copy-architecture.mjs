/** アーキ図(docs/architecture/usecases/*.png)を public/architecture/ へ複製する。
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
  const pngs = readdirSync(src).filter((f) => f.endsWith('.png'))
  for (const f of pngs) cpSync(join(src, f), join(dest, f))
  console.log(`[copy-architecture] copied ${pngs.length} png(s) -> public/architecture/`)
} catch (e) {
  // 図が無くてもビルド自体は通す(ヘルプ画像が出ないだけ)
  console.warn(`[copy-architecture] skipped: ${e.message}`)
}
