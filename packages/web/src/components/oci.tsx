/** OCIコンソール風UI部品(UI-02 / docs/ui/plan.md Phase 2)
 *  色はすべて theme.css の意味トークン(→tokens.cssのRedwood実値)を参照する */
import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { NavIcon, isIconName } from './icons'

/* ---- ボタン: solid(ほぼ黒CTA) / outline / ghost。caretでメニューボタン ---- */
type OciButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'solid' | 'outline' | 'ghost'
  caret?: boolean
}

const BUTTON_VARIANTS = {
  solid: 'bg-cta text-cta-ink hover:bg-cta-strong',
  outline: 'border border-ink/50 text-ink hover:bg-ink/8',
  ghost: 'text-action hover:bg-action-soft',
} as const

export function OciButton({
  variant = 'solid', caret, className = '', children, ...rest
}: OciButtonProps) {
  return (
    <button
      type="button"
      className={`inline-flex items-center gap-1.5 rounded-rw px-3.5 py-1.5 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${BUTTON_VARIANTS[variant]} ${className}`}
      {...rest}
    >
      {children}
      {caret && <span aria-hidden className="text-[9px]">▼</span>}
    </button>
  )
}

/* ---- ステータスピル: アクティブ(緑) / 進行中(黄) / 失敗(赤) / 中立 ---- */
const BADGE_KINDS = {
  ok: 'bg-pill-ok text-pill-ok-ink',
  warn: 'bg-pill-warn text-pill-warn-ink',
  err: 'bg-pill-err text-pill-err-ink',
  neutral: 'border border-line bg-bg text-ink-muted',
} as const

export function StatusBadge({
  kind = 'ok', children,
}: { kind?: keyof typeof BADGE_KINDS; children: ReactNode }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${BADGE_KINDS[kind]}`}
    >
      {children}
    </span>
  )
}

/* ---- パンくず ---- */
export type Crumb = { label: string; to?: string }

export function Breadcrumbs({ items }: { items: Crumb[] }) {
  return (
    <nav aria-label="breadcrumb" className="flex flex-wrap items-center gap-1.5 text-xs">
      {items.map((it, i) => (
        <span key={`${it.label}-${i}`} className="flex items-center gap-1.5">
          {it.to ? (
            <Link to={it.to} className="text-action hover:underline">
              {it.label}
            </Link>
          ) : (
            <span className="text-ink-muted">{it.label}</span>
          )}
          {i < items.length - 1 && (
            <span aria-hidden className="text-ink-muted">
              ›
            </span>
          )}
        </span>
      ))}
    </nav>
  )
}

/* ---- 水平タブ(選択下線) ---- */
export type Tab = { key: string; label: ReactNode }

export function TabBar({
  tabs, active, onChange,
}: { tabs: Tab[]; active: string; onChange: (key: string) => void }) {
  return (
    <div role="tablist" className="flex overflow-x-auto border-b border-line">
      {tabs.map((tb) => (
        <button
          key={tb.key}
          role="tab"
          aria-selected={active === tb.key}
          onClick={() => onChange(tb.key)}
          className={`-mb-px shrink-0 border-b-2 px-4 py-2 text-sm transition-colors ${
            active === tb.key
              ? 'border-action font-semibold text-ink'
              : 'border-transparent text-ink-muted hover:text-ink'
          }`}
        >
          {tb.label}
        </button>
      ))}
    </div>
  )
}

/* ---- テーブル(リンクセル・ピルは render で差し込む) ---- */
export type Column<T> = {
  key: string
  label: ReactNode
  render?: (row: T) => ReactNode
  className?: string
}

export function DataTable<T>({
  columns, rows, rowKey,
}: { columns: Column<T>[]; rows: T[]; rowKey: (row: T) => string }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-left text-sm">
        <thead>
          <tr className="border-b border-line">
            {columns.map((c) => (
              <th
                key={c.key}
                scope="col"
                className={`px-3 py-2 text-xs font-bold text-ink ${c.className ?? ''}`}
              >
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={rowKey(r)} className="border-b border-line last:border-0 hover:bg-bg">
              {columns.map((c) => (
                <td key={c.key} className={`px-3 py-2.5 ${c.className ?? ''}`}>
                  {c.render
                    ? c.render(r)
                    : String((r as Record<string, unknown>)[c.key] ?? '')}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/* ---- カード(docs/ui/screen-example-3.png 準拠): 枠線なし・影・大きめ角丸 ---- */

/** 大カード: 上部にイラスト画像(image指定時) or 色面+アイコンのヘッダ + 太字タイトル + 説明 */
const FEATURE_TONES = {
  terracotta: 'bg-primary',
  green: 'bg-band',
  dark: 'bg-cta',
} as const

export function FeatureCard({
  to, icon, image, title, desc, tone = 'green',
}: {
  to: string
  icon?: string
  image?: string
  title: string
  desc?: string
  tone?: keyof typeof FEATURE_TONES
}) {
  return (
    <Link
      to={to}
      className="group block overflow-hidden rounded-rw-xl bg-surface shadow-rw transition-shadow hover:shadow-rw-md"
    >
      {image ? (
        <img src={image} alt="" className="h-28 w-full object-cover" />
      ) : (
        <div className={`flex h-28 items-center justify-center ${FEATURE_TONES[tone]}`}>
          <span aria-hidden className="text-4xl">{icon}</span>
        </div>
      )}
      <div className="p-4">
        <div className="text-sm font-bold group-hover:text-action">{title}</div>
        {desc && <p className="mt-1 text-xs leading-relaxed text-ink-muted">{desc}</p>}
      </div>
    </Link>
  )
}

/** 小カード: 太字タイトル + 説明、右上にアイコンチップ。dashedは作成系の区別用 */
export function LinkCard({
  to, icon, title, desc, badge, dashed = false,
}: {
  to: string
  icon: string
  title: string
  desc?: string
  badge?: ReactNode
  dashed?: boolean
}) {
  return (
    <Link
      to={to}
      className={`group block rounded-rw-xl p-4 transition-shadow ${
        dashed
          ? 'border border-dashed border-line bg-surface/60 hover:border-action'
          : 'bg-surface shadow-rw hover:shadow-rw-md'
      }`}
    >
      <div className="flex items-center gap-3">
        <span className="min-w-0 flex-1 truncate text-sm font-bold group-hover:text-action">
          {title}
          {badge && <span className="ml-2 text-[10px] font-normal text-ink-muted">{badge}</span>}
        </span>
        <span
          aria-hidden
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-rw bg-band/10 text-lg text-band"
        >
          {isIconName(icon) ? <NavIcon name={icon} className="h-5 w-5" /> : icon}
        </span>
      </div>
      {desc && (
        <p className="mt-2 line-clamp-2 text-xs leading-relaxed text-ink-muted">{desc}</p>
      )}
    </Link>
  )
}

/* ---- 白カード(パネル) ---- */
export function Panel({
  title, action, children, className = '',
}: { title?: ReactNode; action?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`rounded-rw-lg bg-surface shadow-rw ${className}`}>
      {title != null && (
        <header className="flex items-center justify-between gap-3 px-4 pb-2 pt-4">
          <h2 className="text-lg font-bold">{title}</h2>
          {action}
        </header>
      )}
      <div className="px-4 pb-4 pt-1">{children}</div>
    </section>
  )
}
