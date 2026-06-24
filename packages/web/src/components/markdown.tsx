/** Markdownレンダリングの公開ラッパ(P2バンドル分割):
 *  react-markdown/remark-gfm/rehype-highlight/lowlight/mermaid の重量スタックは
 *  MarkdownInner.tsx に隔離し、実際に描画されるまで別チャンクとして読み込まない。
 *  ロード中の一瞬は生のmarkdownソースをそのまま表示し可読性を保つ。
 *  公開API(Md / props)は従来どおりで、利用側(chat/rag/minutes/video/usecase)は無変更。 */
import { lazy, Suspense } from 'react'

const MarkdownInner = lazy(() => import('./MarkdownInner'))

export function Md({ children }: { children: string }) {
  return (
    <Suspense fallback={<div className="whitespace-pre-wrap">{children}</div>}>
      <MarkdownInner>{children}</MarkdownInner>
    </Suspense>
  )
}
