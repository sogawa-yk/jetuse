/* eslint-disable react-refresh/only-export-components -- アイコン群と判定関数を同居 */
/** サイドバー用のモノクロ・ライン系アイコン(UI feedback 2026-06-15)。
 *  絵文字(AIっぽい/多色)をやめ、OCIコンソール調の無機質な線画に統一する。
 *  色は currentColor を継承し、サイズは className(例: h-4 w-4)で制御する。 */
import type { ReactElement, SVGProps } from 'react'

export type IconName =
  | 'home' | 'chat' | 'rag' | 'dbchat' | 'minutes' | 'realtime'
  | 'voicechat' | 'video' | 'ocr' | 'agents' | 'admin' | 'settings' | 'design' | 'market'
  // 汎用(ユースケース/エージェントのアイコン選択用)
  | 'document' | 'edit' | 'translate' | 'link' | 'diagram' | 'search'
  | 'idea' | 'mail' | 'code' | 'image' | 'star' | 'tag' | 'checklist'
  // チャットのコントロール用(ツール/添付。絵文字をやめモノトーンに統一 — feedback 20260618-2)
  | 'tools' | 'attach'

// 24x24・stroke=currentColor・線幅1.6。中身(子要素)だけを定義する
const PATHS: Record<IconName, ReactElement> = {
  home: <path d="M4 11 12 4l8 7M6 10v9h12v-9" />,
  chat: <path d="M4 5h16v10H8l-4 4z" />,
  rag: <path d="M5 4h11l3 3v13H5zM16 4v4h3M8 12h8M8 16h6" />,
  dbchat: (
    <>
      <ellipse cx="12" cy="6" rx="7" ry="2.6" />
      <path d="M5 6v12c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6V6M5 12c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6" />
    </>
  ),
  minutes: <path d="M6 3h9l3 3v15H6zM15 3v4h3M9 12h6M9 16h6M9 8h3" />,
  realtime: (
    <>
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
    </>
  ),
  voicechat: <path d="M4 9v6h4l5 4V5L8 9zM16 9a4 4 0 0 1 0 6M18.5 7a7 7 0 0 1 0 10" />,
  video: <path d="M3 6h12v12H3zM15 10l6-3v10l-6-3z" />,
  ocr: (
    <>
      <path d="M5 3v4M3 5h4M19 3v4M21 5h-4M5 21v-4M3 19h4M19 21v-4M21 19h-4" />
      <rect x="7" y="7" width="10" height="10" rx="1" />
      <path d="M10 11h4M10 14h4" />
    </>
  ),
  agents: (
    <>
      <rect x="6" y="8" width="12" height="10" rx="2" />
      <path d="M12 4v4M9 13h.01M15 13h.01M3 11v3M21 11v3M9 18v2M15 18v2" />
    </>
  ),
  admin: <path d="M4 20V4M4 20h16M8 16v-5M12 16V8M16 16v-8" />,
  settings: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M12 2v3M12 19v3M22 12h-3M5 12H2M19 5l-2 2M7 17l-2 2M19 19l-2-2M7 7 5 5" />
    </>
  ),
  design: (
    <>
      <rect x="4" y="4" width="7" height="7" rx="1" />
      <rect x="13" y="4" width="7" height="7" rx="1" />
      <rect x="4" y="13" width="7" height="7" rx="1" />
      <rect x="13" y="13" width="7" height="7" rx="1" />
    </>
  ),
  market: (
    <>
      <path d="M4 9h16l-1 2.5a2.5 2.5 0 0 1-4.7 0 2.5 2.5 0 0 1-4.6 0 2.5 2.5 0 0 1-4.7 0z" />
      <path d="M5 4h14l1 5M5 4 4 9M5.5 12v8h13v-8M9.5 20v-4h5v4" />
    </>
  ),
  document: <path d="M6 3h8l4 4v14H6zM14 3v4h4M9 12h6M9 16h6" />,
  edit: (
    <>
      <path d="M4 20l1-4L16 5l3 3L8 19z" />
      <path d="M14 7l3 3" />
    </>
  ),
  translate: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M3 12h18M12 3c3.2 2.6 3.2 15.4 0 18M12 3c-3.2 2.6-3.2 15.4 0 18" />
    </>
  ),
  link: (
    <>
      <path d="M9.5 14.5l5-5" />
      <path d="M11 7l1.5-1.5a3.5 3.5 0 015 5L16 12" />
      <path d="M13 17l-1.5 1.5a3.5 3.5 0 01-5-5L8 12" />
    </>
  ),
  diagram: (
    <>
      <rect x="4" y="5" width="6" height="5" rx="1" />
      <rect x="14" y="14" width="6" height="5" rx="1" />
      <path d="M7 10v3a3 3 0 003 3h4" />
    </>
  ),
  search: (
    <>
      <circle cx="11" cy="11" r="6" />
      <path d="M20 20l-4.5-4.5" />
    </>
  ),
  idea: (
    <>
      <path d="M9 18h6M10 21h4" />
      <path d="M8 14a6 6 0 118 0c-.8.8-1.3 1.6-1.5 2.5h-5C9.3 15.6 8.8 14.8 8 14z" />
    </>
  ),
  mail: (
    <>
      <rect x="3" y="5" width="18" height="14" rx="1.5" />
      <path d="M3.5 7l8.5 6 8.5-6" />
    </>
  ),
  code: <path d="M9 8l-4 4 4 4M15 8l4 4-4 4" />,
  image: (
    <>
      <rect x="4" y="5" width="16" height="14" rx="1.5" />
      <path d="M4 16l4.5-4.5 3.5 3.5 3-3 5 5" />
      <circle cx="9" cy="9.5" r="1.2" />
    </>
  ),
  star: <path d="M12 4l2.4 5 5.5.8-4 3.9.9 5.4-4.8-2.5-4.8 2.5.9-5.4-4-3.9 5.5-.8z" />,
  tag: (
    <>
      <path d="M4 4h7l9 9-7 7-9-9z" />
      <circle cx="8" cy="8" r="1.2" />
    </>
  ),
  checklist: (
    <>
      <path d="M10 6h10M10 12h10M10 18h10" />
      <path d="M4 6l1.4 1.4L8 5M4 12l1.4 1.4L8 11M4 18l1.4 1.4L8 17" />
    </>
  ),
  tools: (
    <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
  ),
  attach: (
    <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48" />
  ),
}

/** ユースケース/エージェントのアイコン選択肢(モノトーン・サイドバー統一) */
export const PICKER_ICONS: IconName[] = [
  'document', 'edit', 'translate', 'link', 'diagram', 'chat',
  'search', 'idea', 'mail', 'code', 'image', 'star',
  'tag', 'checklist', 'rag', 'dbchat', 'agents', 'video',
]

/** 選択式アイコンピッカー(controlled)。valueは選択中のアイコン名 */
export function IconPicker({
  value, onChange,
}: { value: string; onChange: (v: string) => void }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {PICKER_ICONS.map((n) => (
        <button
          key={n}
          type="button"
          aria-label={n}
          aria-pressed={value === n}
          onClick={() => onChange(n)}
          className={`flex h-9 w-9 items-center justify-center rounded-rw border transition-colors ${
            value === n
              ? 'border-action bg-action-soft text-action'
              : 'border-line text-ink-muted hover:border-action hover:text-action'
          }`}
        >
          <NavIcon name={n} className="h-5 w-5" />
        </button>
      ))}
    </div>
  )
}

/** icon文字列がライン系アイコン名か判定(ページヘッダのアイコン統一に使用) */
export function isIconName(s: string): s is IconName {
  return Object.prototype.hasOwnProperty.call(PATHS, s)
}

export function NavIcon({
  name, className = 'h-[18px] w-[18px]', ...rest
}: { name: IconName } & SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden
      {...rest}
    >
      {PATHS[name]}
    </svg>
  )
}
