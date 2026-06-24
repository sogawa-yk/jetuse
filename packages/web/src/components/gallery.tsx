import { useEffect, useState, type ReactNode } from 'react'

/* ---------- 基本部品 ---------- */

export function SectionTitle({ children }: { children: ReactNode }) {
  return <h2 className="mb-3 text-sm font-semibold tracking-wide text-ink-muted">{children}</h2>
}

export function Card({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-rw border border-line bg-surface p-5 shadow-rw">{children}</div>
  )
}

// OCIコンソール風: プライマリ操作はほぼ黒のCTA、赤系はdangerに限定(specs/05)
const buttonStyles = {
  primary: 'bg-cta text-cta-ink hover:bg-cta-strong',
  secondary: 'border border-line bg-surface hover:border-action hover:text-action',
  ghost: 'text-ink-muted hover:bg-action-soft hover:text-ink',
  danger: 'border border-line text-primary-strong hover:bg-primary-soft',
} as const

export function Button({
  variant = 'primary', children, ...rest
}: { variant?: keyof typeof buttonStyles } & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      className={`rounded-rw px-4 py-1.5 text-sm font-medium transition-colors
        disabled:cursor-not-allowed disabled:opacity-40 ${buttonStyles[variant]}`}
      {...rest}
    >
      {children}
    </button>
  )
}

/* ---------- チャットバブル ---------- */

const STREAM_TEXT =
  '大阪リージョンで利用可能なモデルは gpt-oss-120b、Command A、Gemini 2.5 Pro / Flash、Llama 3.3 70B です。用途に応じて切り替えてください。'

export function ChatBubbles() {
  const [streamed, setStreamed] = useState('')
  const [playing, setPlaying] = useState(false)

  useEffect(() => {
    if (!playing) return
    let i = 0
    const t = setInterval(() => {
      if (i === 0) setStreamed('')
      i += 2
      setStreamed(STREAM_TEXT.slice(0, i))
      if (i >= STREAM_TEXT.length) {
        setPlaying(false)
        clearInterval(t)
      }
    }, 40)
    return () => clearInterval(t)
  }, [playing])

  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        <div className="max-w-[70%] rounded-rw rounded-tr-none bg-primary px-4 py-2.5 text-sm text-white">
          大阪リージョンで使えるモデルを教えて
        </div>
      </div>
      <div className="flex justify-start gap-2">
        <span className="mt-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-accent text-xs font-bold text-white">
          AI
        </span>
        <div className="max-w-[70%] rounded-rw rounded-tl-none border border-line bg-bg px-4 py-2.5 text-sm leading-relaxed">
          {streamed || STREAM_TEXT}
          {playing && <span className="ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-primary align-text-bottom" />}
          <div className="mt-2 flex gap-2 text-xs text-ink-muted">
            <button className="hover:text-primary" onClick={() => navigator.clipboard.writeText(STREAM_TEXT)}>
              ⧉ コピー
            </button>
            <button className="hover:text-primary" onClick={() => setPlaying(true)}>
              ↻ ストリーミング再生デモ
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

/* ---------- フォーム部品 ---------- */

export function FormParts({ onSubmit }: { onSubmit: () => void }) {
  const [temp, setTemp] = useState(0.7)
  return (
    <form
      className="grid grid-cols-1 gap-4 md:grid-cols-2"
      onSubmit={(e) => { e.preventDefault(); onSubmit() }}
    >
      <label className="block text-sm">
        <span className="mb-1 block font-medium">タイトル</span>
        <input
          type="text" placeholder="例: 週次報告の要約"
          className="w-full rounded-rw border border-line bg-surface px-3 py-2 text-sm
            outline-none focus:border-primary"
        />
      </label>
      <label className="block text-sm">
        <span className="mb-1 block font-medium">モデル</span>
        <select className="w-full rounded-rw border border-line bg-surface px-3 py-2 text-sm outline-none focus:border-primary">
          <option>openai.gpt-oss-120b（標準）</option>
          <option>meta.llama-3.3-70b-instruct（高速）</option>
          <option>google.gemini-2.5-pro（高品質）</option>
          <option>google.gemini-2.5-flash</option>
        </select>
      </label>
      <label className="block text-sm md:col-span-2">
        <span className="mb-1 block font-medium">本文</span>
        <textarea
          rows={3} placeholder="要約したいテキストを貼り付け..."
          className="w-full rounded-rw border border-line bg-surface px-3 py-2 text-sm outline-none focus:border-primary"
        />
      </label>
      <label className="block text-sm">
        <span className="mb-1 block font-medium">Temperature: {temp.toFixed(1)}</span>
        <input
          type="range" min={0} max={1} step={0.1} value={temp}
          onChange={(e) => setTemp(Number(e.target.value))}
          className="w-full accent-(--brand-primary)"
        />
      </label>
      <div className="flex items-end justify-end gap-2">
        <Button variant="secondary" type="button">プレビュー</Button>
        <Button type="submit">実行</Button>
      </div>
    </form>
  )
}

/* ---------- テーブル ---------- */

const ROWS = [
  ['Direct Sales', '¥57,875,260', '45.2%'],
  ['Partners', '¥26,346,342', '20.6%'],
  ['Internet', '¥13,706,802', '10.7%'],
  ['Catalog', '¥9,041,288', '7.1%'],
]

export function Table() {
  return (
    <table className="w-full border-collapse text-sm">
      <thead>
        <tr className="border-b-2 border-line text-left text-xs uppercase tracking-wide text-ink-muted">
          <th className="px-3 py-2">チャネル</th>
          <th className="px-3 py-2 text-right">売上合計</th>
          <th className="px-3 py-2 text-right">構成比</th>
        </tr>
      </thead>
      <tbody>
        {ROWS.map((r) => (
          <tr key={r[0]} className="border-b border-line hover:bg-primary-soft/40">
            <td className="px-3 py-2">{r[0]}</td>
            <td className="px-3 py-2 text-right tabular-nums">{r[1]}</td>
            <td className="px-3 py-2 text-right tabular-nums">{r[2]}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/* ---------- トースト ---------- */

export function Toast({ message, onClose }: { message: string; onClose: () => void }) {
  useEffect(() => {
    const t = setTimeout(onClose, 3500)
    return () => clearTimeout(t)
  }, [onClose])
  return (
    <div className="fixed bottom-6 right-6 flex items-center gap-3 rounded-rw border border-line
      bg-surface px-4 py-3 text-sm shadow-lg">
      <span className="h-2 w-2 rounded-full bg-accent" />
      {message}
      <button onClick={onClose} className="ml-2 text-ink-muted hover:text-ink">✕</button>
    </div>
  )
}
