/** コア同梱 sample-app SBA-A「サポートデスク(問い合わせ管理)業務アプリ」(SBA-02)。
 *
 *  業務フロー（受信トレイ → 問い合わせ詳細 → 対応）を JetUse のデザインシステム/既存部品
 *  (DataTable / StatusBadge / Panel / OciButton / PageContainer)で構成し、AI を業務の自然な
 *  位置に埋め込む:
 *    - 自動トリアージ(classify): カテゴリ＋優先度を AI 提案 →「採用」で反映
 *    - ナレッジ提案(rag.search): FAQ 由来の根拠付き回答候補 →「返信に使う」
 *    - 返信ドラフト(draft): ワンクリック生成 → 編集 → コピー / 送信(デモ)
 *    - スレッド要約(summarize): 長い会話を3行に
 *  AI は既存の runtime bind 機構＋ slot invoke API(POST /api/sample-apps/:id/slots/:key/invoke)を
 *  そのまま流用。コア同梱・DB 不要でデモ完結（問い合わせは取得シードからのローカル業務状態）。 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { authHeaders, reauthenticate, useUser } from '../auth'
import { PageContainer } from '../components/layout'
import { DataTable, OciButton, Panel, StatusBadge, type Column } from '../components/oci'
import { usePrefs } from '../prefs'
import { Nl2SqlApp } from './sampleapp-sba-b'
import SalesDealApp from './sampleappc'

type Field = { name: string; type: string; label?: string | null; required?: boolean }
type Dataset = { name: string; label?: string | null; fields: Field[]; seed: Record<string, unknown>[] }
type AiSlot = { key: string; title: string; capability: string }
type SampleAppDef = { screens: unknown[]; datasets: Dataset[]; aiSlots: AiSlot[]; summary?: string }
type SampleApp = {
  id: string
  name: string
  description?: string
  icon?: string
  knowledge_dataset: string
  slot_bindings: Record<string, boolean>
  definition: SampleAppDef
}

type Inquiry = {
  id: string
  subject: string
  customer: string
  body: string
  thread: string
  category: string
  priority: string
  status: string
  received_at: string
  assignee: string
}
type Faq = { question: string; answer: string; category: string; views: number; updated_at: string }

type Citation = { index: number; label: string; score: number }
type SlotResult = {
  capability: string
  answer?: string
  draft?: string
  summary?: string
  category?: string
  matched?: boolean
  candidates?: string[]
  citations?: Citation[]
  grounded?: boolean
}

type TKey = Parameters<ReturnType<typeof usePrefs>['t']>[0]
type T = (k: TKey) => string

const STATUSES = ['new', 'in_progress', 'on_hold', 'resolved'] as const
const STATUS_KIND: Record<string, 'ok' | 'warn' | 'err' | 'neutral'> = {
  new: 'err',
  in_progress: 'warn',
  resolved: 'ok',
  on_hold: 'neutral',
}
const PRIORITY_KIND: Record<string, 'ok' | 'warn' | 'err' | 'neutral'> = {
  高: 'err',
  中: 'warn',
  低: 'neutral',
}
const ASSIGNEES = ['', '佐々木', '小林', '加藤']

function str(v: unknown): string {
  return typeof v === 'string' ? v : v == null ? '' : String(v)
}
function num(v: unknown): number {
  return typeof v === 'number' ? v : 0
}
function statusLabel(t: T, s: string): string {
  const k = `supportdesk.status.${s}` as TKey
  const label = t(k)
  return label === k ? s : label
}
function fmtDateTime(iso: string): string {
  // ISO(例 2026-06-25T09:12:00)を簡潔表示。パースできなければそのまま。
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso)
  return m ? `${m[2]}/${m[3]} ${m[4]}:${m[5]}` : iso
}

type ThreadMsg = { role: 'customer' | 'agent'; name: string; at: string; text: string }

/** 会話スレッドを発言者ロール付きメッセージ列に正規化する。
 *  正: JSON 配列(seed の構造化スレッド)。後方互換: 旧 "顧客:/担当:" 改行形式の素文字列も
 *  パースに耐える。空なら本文を最初の顧客発言として扱う(本文と会話を一連で読ませる)。 */
function parseThread(
  raw: string,
  fallback: { body: string; customer: string; received_at: string },
): ThreadMsg[] {
  const trimmed = (raw || '').trim()
  if (trimmed.startsWith('[')) {
    try {
      const arr: unknown = JSON.parse(trimmed)
      if (Array.isArray(arr)) {
        const msgs = arr
          .filter((m): m is Record<string, unknown> => !!m && typeof m === 'object')
          .map((m) => ({
            role: m.role === 'agent' ? ('agent' as const) : ('customer' as const),
            name: str(m.name),
            at: str(m.at),
            text: str(m.text),
          }))
          .filter((m) => m.text)
        if (msgs.length) return msgs
      }
    } catch {
      /* 壊れた JSON は下の素文字列パースへフォールバック */
    }
  }
  const lines = trimmed
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean)
  if (lines.length) {
    return lines.map((line) => ({
      role: line.startsWith('担当') ? ('agent' as const) : ('customer' as const),
      name: '',
      at: '',
      text: line.replace(/^(顧客|担当)(（返信）|\(返信\))?\s*[:：]\s*/, '') || line,
    }))
  }
  return [
    { role: 'customer', name: fallback.customer, at: fallback.received_at, text: fallback.body },
  ]
}

/** /sba/:id のディスパッチャ。コア同梱 sample-app は業務UIが大きく異なるため、id ごとに専用
 *  ページへ振り分ける(SBA-C は営業案件管理、それ以外は既定のサポートデスク)。 */
export default function SampleApp() {
  const { id } = useParams()
  if (id === 'builtin-sba-c') return <SalesDealApp />
  return <SupportDeskApp />
}

function SupportDeskApp() {
  const { id } = useParams()
  const { t } = usePrefs()
  const user = useUser()
  const [app, setApp] = useState<SampleApp | null>(null)
  const [loadFail, setLoadFail] = useState<'none' | 'notfound' | 'error'>('none')
  const [loadStatus, setLoadStatus] = useState<number | null>(null)
  const [inquiries, setInquiries] = useState<Inquiry[]>([])
  const [view, setView] = useState<'inbox' | 'detail' | 'knowledge'>('inbox')
  const [selectedId, setSelectedId] = useState<string | null>(null)

  useEffect(() => {
    if (!id) return
    let cancelled = false
    fetch(`/api/sample-apps/${id}`, { headers: authHeaders(user) })
      .then((r) => {
        if (r.status === 401) {
          reauthenticate()
          throw new Error('unauthorized')
        }
        if (!r.ok) throw new Error(String(r.status))
        return r.json()
      })
      .then((d: SampleApp) => {
        if (cancelled) return
        setLoadFail('none')
        setApp(d)
        const ds = Array.isArray(d.definition?.datasets) ? d.definition.datasets : []
        const inq = ds.find((x) => x.name === 'inquiries')
        const rows = Array.isArray(inq?.seed) ? inq.seed : []
        setInquiries(
          rows.map((r, i) => ({
            id: str(r.id) || `inq-${i}`,
            subject: str(r.subject),
            customer: str(r.customer),
            body: str(r.body),
            thread: str(r.thread),
            category: str(r.category),
            priority: str(r.priority),
            status: str(r.status) || 'new',
            received_at: str(r.received_at),
            assignee: '',
          })),
        )
      })
      .catch((e: unknown) => {
        if (cancelled) return
        setApp(null)
        const msg = e instanceof Error ? e.message : ''
        if (msg === 'unauthorized') return
        const status = Number(msg)
        setLoadStatus(Number.isFinite(status) && status > 0 ? status : null)
        setLoadFail(msg === '404' ? 'notfound' : 'error')
      })
    return () => {
      cancelled = true
    }
  }, [id, user])

  const faqs = useMemo<Faq[]>(() => {
    const ds = app?.definition?.datasets ?? []
    const f = Array.isArray(ds) ? ds.find((x) => x.name === 'faqs') : undefined
    const rows = Array.isArray(f?.seed) ? f.seed : []
    return rows.map((r) => ({
      question: str(r.question),
      answer: str(r.answer),
      category: str(r.category),
      views: num(r.views),
      updated_at: str(r.updated_at),
    }))
  }, [app])

  const categories = useMemo(
    () => [...new Set(faqs.map((f) => f.category).filter(Boolean))],
    [faqs],
  )

  // capability → 束縛済み slot key（無ければ null）。
  const slotKeyByCap = useMemo(() => {
    const map: Record<string, string> = {}
    for (const s of app?.definition?.aiSlots ?? []) {
      if (app?.slot_bindings?.[s.key]) map[s.capability] = s.key
    }
    return map
  }, [app])

  const callSlot = useMemo(() => {
    return async (cap: string, input: string, cats?: string[]): Promise<SlotResult> => {
      const slotKey = slotKeyByCap[cap]
      if (!slotKey || !app) throw new Error('slot unavailable')
      const body: Record<string, unknown> = { input }
      if (cats && cats.length) body.categories = cats
      const res = await fetch(`/api/sample-apps/${app.id}/slots/${slotKey}/invoke`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
        body: JSON.stringify(body),
      })
      if (res.status === 401) {
        reauthenticate()
        throw new Error(t('uc.sessionLost'))
      }
      if (!res.ok) {
        const b = (await res.json().catch(() => null)) as { detail?: string } | null
        throw new Error(b?.detail || `HTTP ${res.status}`)
      }
      return (await res.json()) as SlotResult
    }
  }, [app, slotKeyByCap, user, t])

  const updateInquiry = (iid: string, patch: Partial<Inquiry>) =>
    setInquiries((cur) => cur.map((q) => (q.id === iid ? { ...q, ...patch } : q)))

  if (loadFail !== 'none') {
    return (
      <PageContainer icon="💬" title={t('sba.title')}>
        <p className="text-sm text-ink-muted">
          {loadFail === 'notfound' ? t('sba.notFound') : t('sba.loadError')}
          {loadFail === 'error' && loadStatus ? ` (HTTP ${loadStatus})` : ''}
        </p>
        <Link to="/" className="text-sm text-action underline">
          {t('nav.home')}
        </Link>
      </PageContainer>
    )
  }
  if (!app) return null

  // NL2SQL 系の sample-app(SBA-B 在庫・受発注照会)は専用の照会コンソールを描画する。
  // capability で分岐し、SBA-A(サポートデスク)はこの下の既存 UI のまま。
  const caps = new Set((app.definition?.aiSlots ?? []).map((s) => s.capability))
  if (caps.has('nl2sql')) {
    return (
      <PageContainer wide icon={app.icon || '📦'} title={app.name} subtitle={app.description}>
        <Nl2SqlApp app={app} />
      </PageContainer>
    )
  }

  const selected = inquiries.find((q) => q.id === selectedId) ?? null
  const openDetail = (iid: string) => {
    setSelectedId(iid)
    setView('detail')
  }

  const tabBtn = (key: 'inbox' | 'knowledge', label: string) => (
    <button
      type="button"
      onClick={() => setView(key)}
      aria-pressed={view === key}
      className={`rounded-rw px-3 py-1.5 text-sm ${
        view === key
          ? 'bg-action-soft font-medium text-ink'
          : 'border border-line text-ink-muted hover:border-action hover:text-action'
      }`}
    >
      {label}
    </button>
  )

  return (
    <PageContainer
      wide
      icon={app.icon || '💬'}
      title={app.name}
      subtitle={app.description}
      action={
        <div className="flex gap-1.5">
          {tabBtn('inbox', t('supportdesk.inbox'))}
          {tabBtn('knowledge', t('supportdesk.knowledge'))}
        </div>
      }
    >
      {view === 'detail' && selected ? (
        <InquiryDetail
          key={selected.id}
          inquiry={selected}
          categories={categories}
          slotKeyByCap={slotKeyByCap}
          callSlot={callSlot}
          onUpdate={(patch) => updateInquiry(selected.id, patch)}
          onBack={() => setView('inbox')}
          t={t}
        />
      ) : view === 'knowledge' ? (
        <KnowledgeView faqs={faqs} t={t} />
      ) : (
        <InboxView inquiries={inquiries} onOpen={openDetail} t={t} />
      )}
    </PageContainer>
  )
}

/* ---------------- 受信トレイ(非AIの業務UI) ---------------- */

function InboxView({
  inquiries,
  onOpen,
  t,
}: {
  inquiries: Inquiry[]
  onOpen: (id: string) => void
  t: T
}) {
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState('')
  const [category, setCategory] = useState('')
  const q = query.trim().toLowerCase()

  const counts = useMemo(() => {
    const c = { new: 0, in_progress: 0, resolved: 0 }
    for (const i of inquiries) if (i.status in c) c[i.status as keyof typeof c]++
    return c
  }, [inquiries])

  const cats = useMemo(
    () => [...new Set(inquiries.map((i) => i.category).filter(Boolean))],
    [inquiries],
  )

  const rows = inquiries.filter(
    (i) =>
      (!status || i.status === status) &&
      (!category || i.category === category) &&
      (!q ||
        i.subject.toLowerCase().includes(q) ||
        i.customer.toLowerCase().includes(q) ||
        i.body.toLowerCase().includes(q)),
  )

  const columns: Column<Inquiry>[] = [
    {
      key: 'status',
      label: t('supportdesk.col.status'),
      render: (r) => <StatusBadge kind={STATUS_KIND[r.status] ?? 'neutral'}>{statusLabel(t, r.status)}</StatusBadge>,
    },
    {
      key: 'subject',
      label: t('supportdesk.col.subject'),
      render: (r) => (
        <button
          type="button"
          onClick={() => onOpen(r.id)}
          className="text-left font-medium text-action hover:underline"
        >
          {r.subject}
        </button>
      ),
    },
    { key: 'customer', label: t('supportdesk.col.customer'), render: (r) => r.customer },
    {
      key: 'category',
      label: t('supportdesk.col.category'),
      render: (r) => r.category || <span className="text-ink-muted/60">—</span>,
    },
    {
      key: 'priority',
      label: t('supportdesk.col.priority'),
      render: (r) =>
        r.priority ? (
          <StatusBadge kind={PRIORITY_KIND[r.priority] ?? 'neutral'}>{r.priority}</StatusBadge>
        ) : (
          <span className="text-ink-muted/60">—</span>
        ),
    },
    {
      key: 'received_at',
      label: t('supportdesk.col.received'),
      className: 'whitespace-nowrap text-ink-muted',
      render: (r) => fmtDateTime(r.received_at),
    },
  ]

  const stat = (label: string, n: number, kind: 'ok' | 'warn' | 'err') => (
    <div className="rounded-rw-lg border border-line bg-surface px-4 py-3">
      <div className="flex items-center gap-2">
        <StatusBadge kind={kind}>{label}</StatusBadge>
      </div>
      <div className="mt-1 text-2xl font-bold text-ink">{n}</div>
    </div>
  )

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-3 gap-3">
        {stat(statusLabel(t, 'new'), counts.new, 'err')}
        {stat(statusLabel(t, 'in_progress'), counts.in_progress, 'warn')}
        {stat(statusLabel(t, 'resolved'), counts.resolved, 'ok')}
      </div>

      <Panel
        title={`📥 ${t('supportdesk.inbox')}`}
        action={
          <div className="flex flex-wrap items-center gap-2">
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t('supportdesk.search')}
              aria-label={t('supportdesk.search')}
              className="rounded-rw border border-line bg-surface px-2 py-1 text-sm focus:border-action focus:outline-none"
            />
            <select
              value={status}
              onChange={(e) => setStatus(e.target.value)}
              aria-label={t('supportdesk.col.status')}
              className="rounded-rw border border-line bg-surface px-2 py-1 text-sm"
            >
              <option value="">{t('supportdesk.allStatus')}</option>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {statusLabel(t, s)}
                </option>
              ))}
            </select>
            <select
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              aria-label={t('supportdesk.col.category')}
              className="rounded-rw border border-line bg-surface px-2 py-1 text-sm"
            >
              <option value="">{t('supportdesk.allCategory')}</option>
              {cats.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </div>
        }
      >
        {rows.length === 0 ? (
          <p className="py-6 text-center text-sm text-ink-muted">{t('supportdesk.empty')}</p>
        ) : (
          <DataTable columns={columns} rows={rows} rowKey={(r) => r.id} />
        )}
      </Panel>
    </div>
  )
}

/* ---------------- 問い合わせ詳細(業務UIにAI埋込) ---------------- */

function InquiryDetail({
  inquiry,
  categories,
  slotKeyByCap,
  callSlot,
  onUpdate,
  onBack,
  t,
}: {
  inquiry: Inquiry
  categories: string[]
  slotKeyByCap: Record<string, string>
  callSlot: (cap: string, input: string, cats?: string[]) => Promise<SlotResult>
  onUpdate: (patch: Partial<Inquiry>) => void
  onBack: () => void
  t: T
}) {
  const [triage, setTriage] = useState<{
    category: string
    priority: string
    categoryMatched: boolean
    priorityMatched: boolean
  } | null>(null)
  const [knowledge, setKnowledge] = useState<SlotResult | null>(null)
  const [summary, setSummary] = useState<string>('')
  const [draft, setDraft] = useState<string>('')
  const [busy, setBusy] = useState<string>('') // 実行中のアクション名
  const [err, setErr] = useState<string | null>(null)
  const [sent, setSent] = useState(false)
  const [copied, setCopied] = useState(false)
  const draftRef = useRef<HTMLTextAreaElement | null>(null)

  const inquiryInput = `${inquiry.subject}\n${inquiry.body}`
  // 会話スレッド(構造化メッセージ)。本文も会話の最初の発言として一連で読ませる。
  const messages = useMemo(() => parseThread(inquiry.thread, inquiry), [inquiry])
  // 要約 AI へ渡す可読テキスト(JSON ではなく発言者付きの会話文)。
  const threadText = messages
    .map((m) => `${m.role === 'agent' ? '担当' : '顧客'}: ${m.text}`)
    .join('\n')
  const run = async (name: string, fn: () => Promise<void>) => {
    setBusy(name)
    setErr(null)
    try {
      await fn()
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e))
    } finally {
      setBusy('')
    }
  }

  const runTriage = () =>
    run('triage', async () => {
      const cap = await callSlot('classify', inquiryInput, categories)
      const pri = await callSlot('classify', inquiryInput, ['高', '中', '低'])
      // backend は候補一致なし時 matched=false で先頭候補へフォールバックする(カテゴリ/優先度とも)。
      // その推定を「確定の提案」と誤認させないため matched を保持して低信頼表示する。
      setTriage({
        category: cap.category ?? '',
        priority: pri.category ?? '',
        categoryMatched: cap.matched !== false,
        priorityMatched: pri.matched !== false,
      })
    })
  const adoptTriage = () => {
    if (!triage) return
    onUpdate({
      category: triage.category || inquiry.category,
      priority: triage.priority || inquiry.priority,
      status: inquiry.status === 'new' ? 'in_progress' : inquiry.status,
    })
  }

  const runKnowledge = () =>
    run('knowledge', async () => {
      setKnowledge(await callSlot('rag.search', inquiryInput))
    })
  const useInReply = () => {
    if (knowledge?.answer && knowledge.grounded !== false) {
      setDraft(knowledge.answer)
      draftRef.current?.focus()
    }
  }

  const runDraft = () =>
    run('draft', async () => {
      const r = await callSlot('draft', inquiryInput)
      setDraft(r.draft ?? '')
    })
  const runSummary = () =>
    run('summary', async () => {
      const r = await callSlot('summarize', `${inquiry.subject}\n${threadText || inquiry.body}`)
      setSummary(r.summary ?? '')
    })

  const copyDraft = () => {
    setCopied(true)
    try {
      void navigator.clipboard?.writeText(draft)
    } catch {
      /* jsdom など clipboard 無し環境は無視 */
    }
    setTimeout(() => setCopied(false), 1500)
  }
  const sendDraft = () => {
    // 返信ドラフト送信(デモ): 構造化メッセージとして agent ロールでスレッドに追記する。
    const replyAt = new Date().toISOString().slice(0, 19)
    const agentName = inquiry.assignee
      ? `${t('supportdesk.role.agent')} ${inquiry.assignee}`
      : t('supportdesk.role.agent')
    const next: ThreadMsg[] = [
      ...messages,
      { role: 'agent', name: agentName, at: replyAt, text: draft },
    ]
    onUpdate({ status: 'resolved', thread: JSON.stringify(next) })
    setSent(true)
  }

  const setStatus = (s: string) => onUpdate({ status: s })
  const has = (cap: string) => Boolean(slotKeyByCap[cap])

  return (
    <div className="space-y-3">
      <button
        type="button"
        onClick={onBack}
        className="text-sm text-action hover:underline"
      >
        ← {t('supportdesk.back')}
      </button>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        {/* 左: 業務UI(顧客情報/本文/スレッド/ステータス操作/返信) */}
        <div className="space-y-4 lg:col-span-2">
          <Panel title={`📨 ${inquiry.subject}`}>
            <div className="space-y-2 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <StatusBadge kind={STATUS_KIND[inquiry.status] ?? 'neutral'}>
                  {statusLabel(t, inquiry.status)}
                </StatusBadge>
                {inquiry.priority && (
                  <StatusBadge kind={PRIORITY_KIND[inquiry.priority] ?? 'neutral'}>
                    {t('supportdesk.priority')}: {inquiry.priority}
                  </StatusBadge>
                )}
                {inquiry.category && (
                  <span className="rounded-full bg-action-soft px-2 py-0.5 text-xs text-ink">
                    {inquiry.category}
                  </span>
                )}
              </div>
              <div className="text-ink-muted">
                {t('supportdesk.customer')}: <span className="text-ink">{inquiry.customer}</span>
                {inquiry.received_at && <> ・ {fmtDateTime(inquiry.received_at)}</>}
              </div>
              <div className="flex flex-wrap items-center gap-2 pt-1">
                <label className="text-xs text-ink-muted" htmlFor="sd-assignee">
                  {t('supportdesk.assignee')}
                </label>
                <select
                  id="sd-assignee"
                  value={inquiry.assignee}
                  onChange={(e) => onUpdate({ assignee: e.target.value })}
                  className="rounded-rw border border-line bg-surface px-2 py-1 text-sm"
                >
                  {ASSIGNEES.map((a) => (
                    <option key={a || 'none'} value={a}>
                      {a || t('supportdesk.unassigned')}
                    </option>
                  ))}
                </select>
              </div>
              <div className="flex flex-wrap gap-2 pt-1">
                {inquiry.status === 'new' && (
                  <OciButton variant="outline" onClick={() => setStatus('in_progress')}>
                    {t('supportdesk.lifecycle.start')}
                  </OciButton>
                )}
                {(inquiry.status === 'in_progress' || inquiry.status === 'on_hold') && (
                  <OciButton onClick={() => setStatus('resolved')}>
                    {t('supportdesk.lifecycle.resolve')}
                  </OciButton>
                )}
                {inquiry.status === 'in_progress' && (
                  <OciButton variant="ghost" onClick={() => setStatus('on_hold')}>
                    {t('supportdesk.lifecycle.hold')}
                  </OciButton>
                )}
                {inquiry.status === 'resolved' && (
                  <OciButton variant="outline" onClick={() => setStatus('in_progress')}>
                    {t('supportdesk.lifecycle.reopen')}
                  </OciButton>
                )}
              </div>
            </div>
          </Panel>

          <Panel title={`💬 ${t('supportdesk.thread')}`}>
            <div className="space-y-3">
              {messages.map((m, i) => {
                const isAgent = m.role === 'agent'
                const name =
                  m.name ||
                  (isAgent ? t('supportdesk.role.agent') : inquiry.customer || t('supportdesk.role.customer'))
                const initial = (name.trim()[0] ?? (isAgent ? 'S' : 'C')).toUpperCase()
                return (
                  <div
                    key={i}
                    className={`flex gap-2 ${isAgent ? 'flex-row-reverse' : 'flex-row'}`}
                  >
                    <span
                      className={`mt-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-bold ${
                        isAgent ? 'bg-band-chip text-white' : 'bg-action-soft text-ink'
                      }`}
                      aria-hidden="true"
                    >
                      {initial}
                    </span>
                    <div className={`flex max-w-[80%] flex-col ${isAgent ? 'items-end' : 'items-start'}`}>
                      <div className="mb-0.5 flex items-center gap-2 text-[11px] text-ink-muted">
                        <span className="font-medium text-ink">{name}</span>
                        {m.at && <span>{fmtDateTime(m.at)}</span>}
                      </div>
                      <div
                        className={`whitespace-pre-wrap rounded-rw px-3 py-2 text-sm leading-relaxed ${
                          isAgent
                            ? 'rounded-tr-none bg-band text-band-ink'
                            : 'rounded-tl-none border border-line bg-surface text-ink'
                        }`}
                      >
                        {m.text}
                      </div>
                    </div>
                  </div>
                )
              })}
            </div>
          </Panel>

          <Panel
            title={`✍ ${t('supportdesk.draft.title')}`}
            action={
              has('draft') && (
                <OciButton onClick={() => void runDraft()} disabled={busy !== ''}>
                  {busy === 'draft' ? `… ${t('supportdesk.running')}` : `🤖 ${t('supportdesk.draft.generate')}`}
                </OciButton>
              )
            }
          >
            <textarea
              ref={draftRef}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              rows={6}
              aria-label={t('supportdesk.draft.title')}
              placeholder={t('supportdesk.draft.placeholder')}
              className="w-full rounded-rw border border-line bg-surface p-2 text-sm focus:border-action focus:outline-none"
            />
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <OciButton variant="outline" onClick={copyDraft} disabled={!draft}>
                {copied ? `✓ ${t('supportdesk.copied')}` : t('supportdesk.draft.copy')}
              </OciButton>
              <OciButton onClick={sendDraft} disabled={!draft || sent}>
                {sent ? `✓ ${t('supportdesk.sent')}` : t('supportdesk.draft.send')}
              </OciButton>
            </div>
          </Panel>
        </div>

        {/* 右: AI 埋込(トリアージ / ナレッジ提案 / 要約) */}
        <div className="space-y-4">
          {err && (
            <p className="rounded-rw border border-line bg-surface px-3 py-2 text-xs text-primary-strong">
              ⚠ {err}
            </p>
          )}

          {has('classify') && (
            <Panel
              title={`🏷 ${t('supportdesk.triage.title')}`}
              action={
                <OciButton variant="outline" onClick={() => void runTriage()} disabled={busy !== ''}>
                  {busy === 'triage' ? `… ${t('supportdesk.running')}` : t('supportdesk.triage.run')}
                </OciButton>
              }
            >
              {!triage ? (
                <p className="text-xs text-ink-muted">{t('supportdesk.triage.hint')}</p>
              ) : (
                <div className="space-y-2 text-sm">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="rounded-full bg-band/10 px-2 py-0.5 text-[10px] font-medium text-band">
                      🤖 {t('supportdesk.aiSuggested')}
                    </span>
                    <span className="rounded-full bg-action-soft px-2 py-0.5 text-xs text-ink">
                      {t('supportdesk.col.category')}: {triage.category || '—'}
                    </span>
                    <StatusBadge kind={PRIORITY_KIND[triage.priority] ?? 'neutral'}>
                      {t('supportdesk.col.priority')}: {triage.priority || '—'}
                    </StatusBadge>
                  </div>
                  {(!triage.categoryMatched || !triage.priorityMatched) && (
                    <p className="text-[11px] text-pill-warn-ink">⚠ {t('supportdesk.triage.lowConfidence')}</p>
                  )}
                  <OciButton onClick={adoptTriage}>{t('supportdesk.adopt')}</OciButton>
                </div>
              )}
            </Panel>
          )}

          {has('rag.search') && (
            <Panel
              title={`📚 ${t('supportdesk.knowledge.title')}`}
              action={
                <OciButton variant="outline" onClick={() => void runKnowledge()} disabled={busy !== ''}>
                  {busy === 'knowledge' ? `… ${t('supportdesk.running')}` : t('supportdesk.knowledge.run')}
                </OciButton>
              }
            >
              {!knowledge ? (
                <p className="text-xs text-ink-muted">{t('supportdesk.knowledge.hint')}</p>
              ) : knowledge.grounded === false ? (
                <p className="text-sm text-ink-muted">{t('supportdesk.knowledge.none')}</p>
              ) : (
                <div className="space-y-2 text-sm">
                  <p className="whitespace-pre-wrap">{knowledge.answer}</p>
                  {knowledge.citations && knowledge.citations.length > 0 && (
                    <p className="text-[11px] text-ink-muted">
                      {t('supportdesk.knowledge.source')}: {knowledge.citations[0].label}
                    </p>
                  )}
                  <OciButton variant="ghost" onClick={useInReply}>
                    {t('supportdesk.knowledge.useInReply')}
                  </OciButton>
                </div>
              )}
            </Panel>
          )}

          {has('summarize') && (
            <Panel
              title={`📝 ${t('supportdesk.summary.title')}`}
              action={
                <OciButton variant="outline" onClick={() => void runSummary()} disabled={busy !== ''}>
                  {busy === 'summary' ? `… ${t('supportdesk.running')}` : t('supportdesk.summary.run')}
                </OciButton>
              }
            >
              {summary ? (
                <p className="whitespace-pre-wrap text-sm">{summary}</p>
              ) : (
                <p className="text-xs text-ink-muted">{t('supportdesk.summary.hint')}</p>
              )}
            </Panel>
          )}
        </div>
      </div>
    </div>
  )
}

/* ---------------- ナレッジ(FAQ)業務UI ---------------- */

function KnowledgeView({ faqs, t }: { faqs: Faq[]; t: T }) {
  const [query, setQuery] = useState('')
  const q = query.trim().toLowerCase()
  const rows = faqs.filter(
    (f) => !q || f.question.toLowerCase().includes(q) || f.answer.toLowerCase().includes(q),
  )
  const columns: Column<Faq>[] = [
    { key: 'question', label: t('supportdesk.kb.question'), render: (r) => r.question },
    {
      key: 'category',
      label: t('supportdesk.col.category'),
      render: (r) => (
        <span className="rounded-full bg-action-soft px-2 py-0.5 text-xs text-ink">{r.category}</span>
      ),
    },
    {
      key: 'views',
      label: t('supportdesk.kb.views'),
      className: 'whitespace-nowrap text-ink-muted',
      render: (r) => r.views.toLocaleString(),
    },
    {
      key: 'updated_at',
      label: t('supportdesk.kb.updated'),
      className: 'whitespace-nowrap text-ink-muted',
      render: (r) => r.updated_at,
    },
  ]
  return (
    <Panel
      title={`📚 ${t('supportdesk.knowledge')}`}
      action={
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t('supportdesk.search')}
          aria-label={t('supportdesk.search')}
          className="rounded-rw border border-line bg-surface px-2 py-1 text-sm focus:border-action focus:outline-none"
        />
      }
    >
      <p className="mb-2 text-xs text-ink-muted">{t('supportdesk.kb.ragNote')}</p>
      {rows.length === 0 ? (
        <p className="py-6 text-center text-sm text-ink-muted">{t('supportdesk.empty')}</p>
      ) : (
        <DataTable columns={columns} rows={rows} rowKey={(r) => r.question} />
      )}
    </Panel>
  )
}
