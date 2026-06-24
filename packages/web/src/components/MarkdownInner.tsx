/** Markdownレンダリング本体(CHAT-03b/03c):
 *  rehype-highlight(+カスタムHCL文法) + Mermaid(動的import・parse検証・自動修復)
 *  react-markdown/remark-gfm/rehype-highlight/lowlight の重量スタックを内包し、
 *  markdown.tsx の React.lazy 経由で別チャンクとして遅延ロードされる(P2バンドル分割)。 */
import type { HLJSApi, Language } from 'highlight.js'
import { isValidElement, useEffect, useId, useState, type ReactNode } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import rehypeHighlight from 'rehype-highlight'
import remarkGfm from 'remark-gfm'
import { common } from 'lowlight'
import 'highlight.js/styles/github-dark.css'
import { usePrefs } from '../prefs'

/** Terraform/HCL文法(CHAT-03c)。highlight.js本体に存在しないため最小定義を登録 */
function hcl(hljs: HLJSApi): Language {
  return {
    name: 'Terraform',
    aliases: ['tf', 'hcl'],
    keywords: {
      keyword:
        'resource variable provider output data module locals terraform dynamic ' +
        'for in if else for_each count depends_on lifecycle backend required_providers',
      literal: 'true false null',
    },
    contains: [
      hljs.COMMENT(/#/, /$/),
      hljs.COMMENT(/\/\//, /$/),
      hljs.C_BLOCK_COMMENT_MODE,
      { className: 'number', begin: /\b\d+(\.\d+)?\b/ },
      {
        className: 'string',
        begin: /"/,
        end: /"/,
        contains: [{ className: 'subst', begin: /\$\{/, end: /\}/ }],
      },
      { className: 'attr', begin: /[\w-]+(?=\s*=[^=>])/ },
    ],
  }
}

/** ハイライトでspan分割されたchildrenからプレーンテキストを復元する */
function textOf(n: ReactNode): string {
  if (typeof n === 'string' || typeof n === 'number') return String(n)
  if (Array.isArray(n)) return n.map(textOf).join('')
  if (isValidElement<{ children?: ReactNode }>(n)) return textOf(n.props.children)
  return ''
}

/** LLM頻出のmermaid構文ミス(ノードラベル内の未クオート括弧)を自動クオートする。
 *  parseが通った場合のみ採用するためレンダリング誤りの心配はない(CHAT-03c) */
function repairMermaid(src: string): string {
  return src
    .replace(/\[([^[\]"|]*[()（）][^[\]"|]*)\]/g, '["$1"]')
    .replace(/\{([^{}"|]*[()（）][^{}"|]*)\}/g, '{"$1"}')
}

function MermaidBlock({ code }: { code: string }) {
  const { dark } = usePrefs()
  const id = useId().replace(/[^a-zA-Z0-9]/g, '')
  const [svg, setSvg] = useState<string | null>(null)
  const [invalid, setInvalid] = useState(false)

  useEffect(() => {
    let alive = true
    // ストリーミング中の未完成ソース対策: 少し待ってからparse検証が通った時のみ描画
    const timer = setTimeout(async () => {
      try {
        const mermaid = (await import('mermaid')).default
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: 'strict',
          theme: dark ? 'dark' : 'default',
        })
        let src = code
        if (!(await mermaid.parse(src, { suppressErrors: true }))) {
          src = repairMermaid(code) // 未クオート括弧の自動修復を試す
          if (!(await mermaid.parse(src, { suppressErrors: true }))) {
            if (alive) {
              setSvg(null)
              setInvalid(true)
            }
            return
          }
        }
        const out = await mermaid.render(`mmd${id}`, src)
        if (alive) {
          setSvg(out.svg)
          setInvalid(false)
        }
      } catch {
        if (alive) {
          setSvg(null)
          setInvalid(true)
        }
        document.getElementById(`dmmd${id}`)?.remove() // render失敗時の残骸除去
      }
    }, 200)
    return () => {
      alive = false
      clearTimeout(timer)
    }
  }, [code, dark, id])

  if (!svg) {
    return (
      <>
        <pre>
          <code>{code}</code>
        </pre>
        {invalid && (
          <p className="-mt-1 text-xs text-ink-muted">
            ⚠ Mermaid構文エラーのためコードを表示しています（生成内容の問題）
          </p>
        )}
      </>
    )
  }
  return (
    <div
      className="mermaid-svg my-2 overflow-x-auto rounded-rw border border-line bg-surface p-2"
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  )
}

const components: Components = {
  pre({ children, ...props }) {
    const child = Array.isArray(children) ? children[0] : children
    if (
      isValidElement<{ className?: string; children?: ReactNode }>(child) &&
      child.props.className?.includes('language-mermaid')
    ) {
      return <MermaidBlock code={textOf(child.props.children)} />
    }
    return <pre {...props}>{children}</pre>
  },
}

const highlightOptions = {
  detect: false,
  languages: { ...common, terraform: hcl },
  aliases: { terraform: ['tf', 'hcl'] },
}

export default function MarkdownInner({ children }: { children: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[[rehypeHighlight, highlightOptions]]}
      components={components}
    >
      {children}
    </ReactMarkdown>
  )
}
