import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import featureChat from '../assets/feature-chat.webp'
import featureDb from '../assets/feature-db.webp'
import featureRag from '../assets/feature-rag.webp'
import { authHeaders, useUser } from '../auth'
import { NavIcon, isIconName } from '../components/icons'
import { PageContainer } from '../components/layout'
import { FeatureCard, LinkCard } from '../components/oci'
import { usePrefs } from '../prefs'

type UcSummary = {
  id: string
  name: string
  description?: string | null
  icon?: string | null
  tags?: string[]
  builtin?: boolean
  visibility?: string
  mine?: boolean
}
type AgentSummary = {
  id: string
  name: string
  description?: string | null
  icon?: string | null
  mine?: boolean
  visibility?: string
}

// 並び替え順の永続化(この端末のみ。プロトタイプ範囲・DB移行不要)
const ORDER_KEY = 'jetuse.homeOrder'
const loadOrder = (): string[] => {
  try {
    const v = JSON.parse(localStorage.getItem(ORDER_KEY) ?? '[]')
    return Array.isArray(v) ? v : []
  } catch {
    return []
  }
}

type Item =
  | { kind: 'uc'; key: string; uc: UcSummary }
  | { kind: 'agent'; key: string; agent: AgentSummary }

export default function Home() {
  const { t } = usePrefs()
  const user = useUser()
  const [usecases, setUsecases] = useState<UcSummary[]>([])
  const [agents, setAgents] = useState<AgentSummary[]>([])
  const [tag, setTag] = useState('')
  const [reorder, setReorder] = useState(false)
  const [order, setOrder] = useState<string[]>(loadOrder)
  const [searchParams] = useSearchParams()
  // ホーム内の検索ボックス(サイト横断ではなくホームのカードを絞り込む)
  const [query, setQuery] = useState(() => searchParams.get('q') ?? '')
  const q = query.trim().toLowerCase()
  const searching = q.length > 0
  const matchQ = (name: string, description?: string | null) =>
    !q || name.toLowerCase().includes(q) || (description ?? '').toLowerCase().includes(q)

  useEffect(() => {
    fetch('/api/usecases', { headers: authHeaders(user) })
      .then((r) => r.json())
      .then((d) => setUsecases(d.usecases))
      .catch(() => setUsecases([]))
    fetch('/api/agents', { headers: authHeaders(user) })
      .then((r) => r.json())
      .then((d) => setAgents(d.agents ?? []))
      .catch(() => setAgents([]))
  }, [user])

  const tags = [...new Set(usecases.flatMap((u) => u.tags ?? []))]

  // ユースケース+エージェントを保存順に整列(未登録の項目は末尾、相対順は維持)
  const ordered = useMemo<Item[]>(() => {
    const items: Item[] = [
      ...usecases.map((u) => ({ kind: 'uc' as const, key: `uc:${u.id}`, uc: u })),
      ...agents.map((a) => ({ kind: 'agent' as const, key: `agent:${a.id}`, agent: a })),
    ]
    const rank = new Map(order.map((k, i) => [k, i]))
    return [...items].sort(
      (a, b) => (rank.get(a.key) ?? Infinity) - (rank.get(b.key) ?? Infinity),
    )
  }, [usecases, agents, order])

  // 絞り込みは並び替えモードでない時だけ適用(並び替え中は全件を対象にする)
  const visible = reorder
    ? ordered
    : ordered.filter((it) =>
        it.kind === 'uc'
          ? (!tag || (it.uc.tags ?? []).includes(tag)) && matchQ(it.uc.name, it.uc.description)
          : matchQ(it.agent.name, it.agent.description),
      )

  const move = (key: string, dir: -1 | 1) => {
    const keys = ordered.map((it) => it.key)
    const i = keys.indexOf(key)
    const j = i + dir
    if (i < 0 || j < 0 || j >= keys.length) return
    ;[keys[i], keys[j]] = [keys[j], keys[i]]
    setOrder(keys)
    localStorage.setItem(ORDER_KEY, JSON.stringify(keys))
  }

  // 標準3種(大カード)。検索時は名前/説明でこちらも絞る
  const features = [
    { to: '/chat', image: featureChat, title: t('nav.chat'), desc: t('home.chat.desc') },
    { to: '/rag', image: featureRag, title: t('nav.rag'), desc: t('home.rag.desc') },
    { to: '/dbchat', image: featureDb, title: t('nav.dbchat'), desc: t('home.db.desc') },
  ] as const
  const shownFeatures = reorder ? [] : features.filter((f) => matchQ(f.title, f.desc))

  return (
    <PageContainer
      wide
      icon={searching ? 'search' : 'home'}
      title={searching ? t('home.searchResults') : t('home.title')}
      subtitle={searching ? undefined : t('home.lead')}
      action={
        <button
          onClick={() => setReorder((v) => !v)}
          className={`rounded-rw border px-3 py-1.5 text-sm ${
            reorder
              ? 'border-action bg-action-soft text-ink'
              : 'border-line text-ink-muted hover:border-action hover:text-action'
          }`}
        >
          {reorder ? `✓ ${t('home.reorderDone')}` : `⇅ ${t('home.reorder')}`}
        </button>
      }
    >
      {!reorder && (
        <div className="mb-4 flex items-center gap-2 rounded-full border border-line bg-surface px-4 py-2 text-sm focus-within:border-action">
          <span aria-hidden className="text-ink-muted">⌕</span>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t('home.searchPh')}
            className="w-full bg-transparent placeholder:text-ink-muted focus:outline-none"
          />
          {query && (
            <button
              onClick={() => setQuery('')}
              aria-label={t('home.clearSearch')}
              className="shrink-0 text-ink-muted hover:text-ink"
            >
              ✕
            </button>
          )}
        </div>
      )}
      {reorder && (
        <p className="mb-3 rounded-rw border border-action bg-action-soft px-3 py-2 text-xs text-ink-muted">
          {t('home.reorderHint')}
        </p>
      )}
      {!reorder && tags.length > 0 && (
        <div className="mb-4 flex flex-wrap items-center gap-1.5 text-xs">
          <button
            onClick={() => setTag('')}
            className={`rounded-full px-2.5 py-1 ${!tag ? 'bg-action-soft font-medium' : 'border border-line hover:border-action'}`}
          >
            {t('home.allTags')}
          </button>
          {tags.map((tg) => (
            <button
              key={tg}
              onClick={() => setTag(tg === tag ? '' : tg)}
              className={`rounded-full px-2.5 py-1 ${tg === tag ? 'bg-action-soft font-medium' : 'border border-line hover:border-action'}`}
            >
              {tg}
            </button>
          ))}
        </div>
      )}

      {/* 標準ユースケース(イラスト風ヘッダの大カード) */}
      {shownFeatures.length > 0 && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4">
          {shownFeatures.map((f) => (
            <FeatureCard key={f.to} to={f.to} image={f.image} title={f.title} desc={f.desc} />
          ))}
        </div>
      )}

      {/* ユースケース・エージェント・作成(小カード) */}
      <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4">
        {visible.map((it) =>
          reorder ? (
            <ReorderCard
              key={it.key}
              icon={it.kind === 'uc' ? it.uc.icon || '🧩' : it.agent.icon || '🤖'}
              title={it.kind === 'uc' ? it.uc.name : it.agent.name}
              onUp={() => move(it.key, -1)}
              onDown={() => move(it.key, 1)}
              upLabel={t('home.moveUp')}
              downLabel={t('home.moveDown')}
            />
          ) : it.kind === 'uc' ? (
            <LinkCard
              key={it.key}
              to={`/uc/${it.uc.id}`}
              icon={it.uc.icon || '🧩'}
              title={it.uc.name}
              desc={it.uc.description ?? undefined}
              badge={
                <>
                  {it.uc.builtin
                    ? t('home.builtin')
                    : it.uc.visibility === 'public'
                      ? t('home.shared')
                      : ''}
                  {it.uc.mine && (
                    <Link
                      to={`/builder/${it.uc.id}`}
                      onClick={(e) => e.stopPropagation()}
                      className="ml-1.5 underline hover:text-action"
                    >
                      {t('uc.edit')}
                    </Link>
                  )}
                </>
              }
            />
          ) : (
            <LinkCard
              key={it.key}
              to={`/chat?agent=${it.agent.id}`}
              icon={it.agent.icon || '🤖'}
              title={it.agent.name}
              desc={it.agent.description ?? undefined}
              badge={
                <>
                  {it.agent.mine
                    ? it.agent.visibility === 'public'
                      ? t('home.shared')
                      : ''
                    : t('home.shared')}
                  {it.agent.mine && (
                    <Link
                      to={`/agents/${it.agent.id}`}
                      onClick={(e) => e.stopPropagation()}
                      className="ml-1.5 underline hover:text-action"
                    >
                      {t('uc.edit')}
                    </Link>
                  )}
                </>
              }
            />
          ),
        )}
        {/* 「作成」カード・議事録は導線であり検索結果ではない。
            検索中は常時表示しない(feedback 20260618-3 #2) */}
        {!reorder && !searching && (
          <>
            <LinkCard
              to="/agents/new"
              icon="agents"
              title={`＋ ${t('home.newAgent')}`}
              desc={t('home.newAgent.desc')}
              dashed
            />
            <LinkCard
              to="/builder"
              icon="🛠"
              title={`＋ ${t('home.newUsecase')}`}
              desc={t('home.newUsecase.desc')}
              dashed
            />
            <LinkCard to="/minutes" icon="minutes" title={t('nav.minutes')} desc={t('minutes.desc')} />
          </>
        )}
      </div>
    </PageContainer>
  )
}

/** 並び替えモードのカード: リンクではなく▲▼ボタンで順序変更 */
function ReorderCard({
  icon, title, onUp, onDown, upLabel, downLabel,
}: {
  icon: string
  title: string
  onUp: () => void
  onDown: () => void
  upLabel: string
  downLabel: string
}) {
  return (
    <div className="flex items-center gap-3 rounded-rw-xl border border-dashed border-action bg-surface p-4">
      <span
        aria-hidden
        className="flex h-9 w-9 shrink-0 items-center justify-center rounded-rw bg-band/10 text-lg"
      >
        {isIconName(icon) ? <NavIcon name={icon} className="h-5 w-5" /> : icon}
      </span>
      <span className="min-w-0 flex-1 truncate text-sm font-bold">{title}</span>
      <span className="flex shrink-0 gap-1">
        <button
          onClick={onUp}
          aria-label={upLabel}
          title={upLabel}
          className="rounded-rw border border-line px-2 py-1 text-sm hover:border-action hover:text-action"
        >
          ▲
        </button>
        <button
          onClick={onDown}
          aria-label={downLabel}
          title={downLabel}
          className="rounded-rw border border-line px-2 py-1 text-sm hover:border-action hover:text-action"
        >
          ▼
        </button>
      </span>
    </div>
  )
}
