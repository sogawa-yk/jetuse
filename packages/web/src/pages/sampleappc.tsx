/** コア同梱 sample-app SBA-C「営業案件管理(SFA-lite)」(SBA-04)。
 *
 *  営業の業務フロー（パイプライン → 案件コンソール → 売上分析）に、複合AIを「連動」させた
 *  リファレンス業務アプリ。既存の slot invoke API(POST /api/sample-apps/:id/slots/:key/invoke)を
 *  そのまま流用し、AGT/VOICE/NL2SQL の能力を組込点に配置する:
 *    - 議事録要約(minutes): 商談メモを構造化要約
 *    - 次アクション提案(agent): 案件情報＋議事録要約から次アクションを提案
 *    - 売上集計(nl2sql): 自然言語の集計依頼を専用スキーマ JETUSE_SBA04 へ照会
 *    - フォローメール下書き(draft): 顧客向けメールを下書き（承認制・外部送信なし）
 *  「連動」= 議事録要約 → 次アクション → メール下書き、と前段の出力を後段の入力へ渡す。 */
import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { authHeaders, reauthenticate, useUser } from '../auth'
import { PageContainer } from '../components/layout'
import { DataTable, OciButton, Panel, StatusBadge, type Column } from '../components/oci'

type Field = { name: string; type: string; label?: string | null; required?: boolean }
type Dataset = { name: string; label?: string | null; fields: Field[]; seed: Record<string, unknown>[] }
type AiSlot = { key: string; title: string; capability: string }
type SampleAppDef = { screens: unknown[]; datasets: Dataset[]; aiSlots: AiSlot[]; summary?: string }
type SampleApp = {
  id: string
  name: string
  description?: string
  icon?: string
  slot_bindings: Record<string, boolean>
  definition: SampleAppDef
}

type Deal = {
  id: string
  name: string
  customer: string
  stage: string
  amount: number
  probability: number
  owner: string
  close_date: string
  next_step: string
}
type Meeting = {
  id: string
  deal_id: string
  title: string
  date: string
  attendees: string
  notes: string
}

type SlotResult = {
  capability: string
  summary?: string
  actions?: string[]
  text?: string
  draft?: string
  schema?: string
  sql?: string
  columns?: string[]
  rows?: string[][]
  row_count?: number
  truncated?: boolean
}

const STAGE_KIND: Record<string, 'ok' | 'warn' | 'err' | 'neutral'> = {
  受注: 'ok',
  交渉: 'warn',
  見積: 'warn',
  提案: 'neutral',
  リード: 'neutral',
  失注: 'err',
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : v == null ? '' : String(v)
}
function num(v: unknown): number {
  return typeof v === 'number' ? v : Number(v) || 0
}
function yen(n: number): string {
  return `¥${n.toLocaleString()}`
}

/** 売上集計(NL2SQL)のプリセット質問。実 ADB の専用スキーマ JETUSE_SBA04 を照会する。 */
const ROLLUP_PRESETS = [
  '担当者別の売上合計を多い順に',
  '地域別の売上合計を多い順に',
  '製品別の売上件数と合計金額',
]

export default function SalesDealApp() {
  const { id } = useParams()
  const user = useUser()
  const [app, setApp] = useState<SampleApp | null>(null)
  const [loadFail, setLoadFail] = useState<'none' | 'notfound' | 'error'>('none')
  const [view, setView] = useState<'pipeline' | 'deal' | 'analytics'>('pipeline')
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
      })
      .catch((e: unknown) => {
        if (cancelled) return
        setApp(null)
        const msg = e instanceof Error ? e.message : ''
        if (msg === 'unauthorized') return
        setLoadFail(msg === '404' ? 'notfound' : 'error')
      })
    return () => {
      cancelled = true
    }
  }, [id, user])

  const datasets = useMemo(() => app?.definition?.datasets ?? [], [app])
  const deals = useMemo<Deal[]>(() => {
    const ds = datasets.find((x) => x.name === 'deals')
    const rows = Array.isArray(ds?.seed) ? ds.seed : []
    return rows.map((r) => ({
      id: str(r.id),
      name: str(r.name),
      customer: str(r.customer),
      stage: str(r.stage),
      amount: num(r.amount),
      probability: num(r.probability),
      owner: str(r.owner),
      close_date: str(r.close_date),
      next_step: str(r.next_step),
    }))
  }, [datasets])
  const meetings = useMemo<Meeting[]>(() => {
    const ds = datasets.find((x) => x.name === 'meetings')
    const rows = Array.isArray(ds?.seed) ? ds.seed : []
    return rows.map((r) => ({
      id: str(r.id),
      deal_id: str(r.deal_id),
      title: str(r.title),
      date: str(r.date),
      attendees: str(r.attendees),
      notes: str(r.notes),
    }))
  }, [datasets])

  const slotKeyByCap = useMemo(() => {
    const map: Record<string, string> = {}
    for (const s of app?.definition?.aiSlots ?? []) {
      if (app?.slot_bindings?.[s.key]) map[s.capability] = s.key
    }
    return map
  }, [app])

  const callSlot = useMemo(() => {
    return async (cap: string, input: string): Promise<SlotResult> => {
      const slotKey = slotKeyByCap[cap]
      if (!slotKey || !app) throw new Error('この機能は利用できません')
      const res = await fetch(`/api/sample-apps/${app.id}/slots/${slotKey}/invoke`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
        body: JSON.stringify({ input }),
      })
      if (res.status === 401) {
        reauthenticate()
        throw new Error('セッションが切れました')
      }
      if (!res.ok) {
        const b = (await res.json().catch(() => null)) as { detail?: string } | null
        throw new Error(b?.detail || `HTTP ${res.status}`)
      }
      return (await res.json()) as SlotResult
    }
  }, [app, slotKeyByCap, user])

  if (loadFail !== 'none') {
    return (
      <PageContainer icon="📊" title="営業案件管理">
        <p className="text-sm text-ink-muted">
          {loadFail === 'notfound'
            ? 'サンプルアプリが見つかりません'
            : 'サンプルアプリの読み込みに失敗しました。'}
        </p>
        <Link to="/" className="text-sm text-action underline">
          ホーム
        </Link>
      </PageContainer>
    )
  }
  if (!app) return null

  const selected = deals.find((d) => d.id === selectedId) ?? null
  const openDeal = (did: string) => {
    setSelectedId(did)
    setView('deal')
  }

  const tabBtn = (key: 'pipeline' | 'analytics', label: string) => (
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
      icon={app.icon || '📊'}
      title={app.name}
      subtitle={app.description}
      action={
        <div className="flex gap-1.5">
          {tabBtn('pipeline', '案件パイプライン')}
          {tabBtn('analytics', '売上分析')}
        </div>
      }
    >
      {view === 'deal' && selected ? (
        <DealConsole
          key={selected.id}
          deal={selected}
          meetings={meetings.filter((m) => m.deal_id === selected.id)}
          slotKeyByCap={slotKeyByCap}
          callSlot={callSlot}
          onBack={() => setView('pipeline')}
        />
      ) : view === 'analytics' ? (
        <AnalyticsView
          hasRollup={Boolean(slotKeyByCap['nl2sql'])}
          callSlot={callSlot}
        />
      ) : (
        <PipelineView deals={deals} onOpen={openDeal} />
      )}
    </PageContainer>
  )
}

/* ---------------- パイプライン(非AIの業務UI) ---------------- */

function PipelineView({ deals, onOpen }: { deals: Deal[]; onOpen: (id: string) => void }) {
  const [owner, setOwner] = useState('')
  const owners = useMemo(() => [...new Set(deals.map((d) => d.owner).filter(Boolean))], [deals])
  const rows = deals.filter((d) => !owner || d.owner === owner)

  const totals = useMemo(() => {
    const open = deals.filter((d) => d.stage !== '受注' && d.stage !== '失注')
    const pipeline = open.reduce((s, d) => s + d.amount, 0)
    const weighted = open.reduce((s, d) => s + (d.amount * d.probability) / 100, 0)
    const won = deals.filter((d) => d.stage === '受注').reduce((s, d) => s + d.amount, 0)
    return { pipeline, weighted: Math.round(weighted), won }
  }, [deals])

  const columns: Column<Deal>[] = [
    {
      key: 'name',
      label: '案件',
      render: (r) => (
        <button
          type="button"
          onClick={() => onOpen(r.id)}
          className="text-left font-medium text-action hover:underline"
        >
          {r.name}
        </button>
      ),
    },
    { key: 'customer', label: '顧客', render: (r) => r.customer },
    {
      key: 'stage',
      label: 'ステージ',
      render: (r) => <StatusBadge kind={STAGE_KIND[r.stage] ?? 'neutral'}>{r.stage}</StatusBadge>,
    },
    { key: 'amount', label: '金額', className: 'whitespace-nowrap', render: (r) => yen(r.amount) },
    {
      key: 'probability',
      label: '確度',
      className: 'whitespace-nowrap text-ink-muted',
      render: (r) => `${r.probability}%`,
    },
    { key: 'owner', label: '担当', render: (r) => r.owner },
    {
      key: 'close_date',
      label: '完了予定',
      className: 'whitespace-nowrap text-ink-muted',
      render: (r) => r.close_date,
    },
  ]

  const stat = (label: string, n: number, kind: 'ok' | 'warn' | 'err' | 'neutral') => (
    <div className="rounded-rw-lg border border-line bg-surface px-4 py-3">
      <StatusBadge kind={kind}>{label}</StatusBadge>
      <div className="mt-1 text-2xl font-bold text-ink">{yen(n)}</div>
    </div>
  )

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-3 gap-3">
        {stat('パイプライン総額', totals.pipeline, 'warn')}
        {stat('加重予測', totals.weighted, 'neutral')}
        {stat('受注済み', totals.won, 'ok')}
      </div>
      <Panel
        title="📈 案件パイプライン"
        action={
          <select
            value={owner}
            onChange={(e) => setOwner(e.target.value)}
            aria-label="担当で絞り込み"
            className="rounded-rw border border-line bg-surface px-2 py-1 text-sm"
          >
            <option value="">すべての担当</option>
            {owners.map((o) => (
              <option key={o} value={o}>
                {o}
              </option>
            ))}
          </select>
        }
      >
        {rows.length === 0 ? (
          <p className="py-6 text-center text-sm text-ink-muted">該当する案件はありません</p>
        ) : (
          <DataTable columns={columns} rows={rows} rowKey={(r) => r.id} />
        )}
      </Panel>
    </div>
  )
}

/* ---------------- 案件コンソール(業務UIに複合AIを連動) ---------------- */

function DealConsole({
  deal,
  meetings,
  slotKeyByCap,
  callSlot,
  onBack,
}: {
  deal: Deal
  meetings: Meeting[]
  slotKeyByCap: Record<string, string>
  callSlot: (cap: string, input: string) => Promise<SlotResult>
  onBack: () => void
}) {
  const [meetingId, setMeetingId] = useState(meetings[0]?.id ?? '')
  const [summary, setSummary] = useState('')
  const [actions, setActions] = useState<string[]>([])
  const [rollup, setRollup] = useState<{ columns: string[]; rows: string[][] } | null>(null)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [sent, setSent] = useState(false)

  const meeting = meetings.find((m) => m.id === meetingId) ?? null
  const has = (cap: string) => Boolean(slotKeyByCap[cap])
  const dealCtx =
    `案件名: ${deal.name}\n顧客: ${deal.customer}\nステージ: ${deal.stage}\n` +
    `金額: ${yen(deal.amount)} / 確度: ${deal.probability}%\n次ステップ: ${deal.next_step}`

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

  // 1) 議事録要約
  const runSummary = () =>
    run('summary', async () => {
      if (!meeting) throw new Error('議事録が選択されていません')
      const r = await callSlot('minutes', `${meeting.title}\n${meeting.notes}`)
      setSummary(r.summary ?? '')
    })
  // 2) 次アクション提案(議事録要約を連動入力に)
  const runActions = () =>
    run('actions', async () => {
      const input = summary
        ? `${dealCtx}\n\n【議事録要約】\n${summary}`
        : dealCtx
      const r = await callSlot('agent', input)
      setActions(r.actions ?? [])
    })
  // 3) 売上集計(NL2SQL)を案件コンソールに連動(担当者別売上を専用スキーマへ照会)
  const runRollup = () =>
    run('rollup', async () => {
      const r = await callSlot('nl2sql', '担当者別の売上合計を多い順に')
      setRollup({ columns: r.columns ?? [], rows: r.rows ?? [] })
    })
  // 売上集計結果を下書き入力へ織り込むための短い要約(先頭数行)。
  const rollupForDraft = () => {
    if (!rollup || !rollup.rows.length) return ''
    const head = rollup.columns.join(' / ')
    const top = rollup.rows.slice(0, 3).map((r) => r.join(' / ')).join('\n')
    return `\n\n【売上参考(${head})】\n${top}`
  }
  // 4) フォローメール下書き(案件＋次アクション＋売上集計を連動入力に)
  const runDraft = () =>
    run('draft', async () => {
      const acts = actions.length ? `\n\n【想定アクション】\n${actions.join('\n')}` : ''
      const r = await callSlot(
        'draft',
        `${deal.customer} 宛のフォローメール。${dealCtx}${acts}${rollupForDraft()}`,
      )
      setDraft(r.draft ?? '')
      setSent(false)
    })

  return (
    <div className="space-y-3">
      <button type="button" onClick={onBack} className="text-sm text-action hover:underline">
        ← パイプラインに戻る
      </button>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        {/* 左: 案件情報＋議事録 */}
        <div className="space-y-4 lg:col-span-2">
          <Panel title={`💼 ${deal.name}`}>
            <div className="space-y-1.5 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <StatusBadge kind={STAGE_KIND[deal.stage] ?? 'neutral'}>{deal.stage}</StatusBadge>
                <span className="rounded-full bg-action-soft px-2 py-0.5 text-xs text-ink">
                  {yen(deal.amount)} ・ 確度 {deal.probability}%
                </span>
              </div>
              <div className="text-ink-muted">
                顧客: <span className="text-ink">{deal.customer}</span> ・ 担当: {deal.owner} ・
                完了予定: {deal.close_date}
              </div>
              <div className="text-ink-muted">次ステップ: {deal.next_step}</div>
            </div>
          </Panel>

          <Panel
            title="🗒 商談議事録"
            action={
              meetings.length > 1 && (
                <select
                  value={meetingId}
                  onChange={(e) => setMeetingId(e.target.value)}
                  aria-label="議事録を選択"
                  className="rounded-rw border border-line bg-surface px-2 py-1 text-sm"
                >
                  {meetings.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.title}
                    </option>
                  ))}
                </select>
              )
            }
          >
            {meeting ? (
              <div className="space-y-1 text-sm">
                <div className="text-ink-muted">
                  {meeting.date} ・ {meeting.attendees}
                </div>
                <p className="whitespace-pre-wrap leading-relaxed">{meeting.notes}</p>
              </div>
            ) : (
              <p className="text-xs text-ink-muted">この案件に紐づく議事録はありません</p>
            )}
          </Panel>

          {has('draft') && (
            <Panel title="✉ フォローメール下書き（承認制・外部送信なし）">
              <textarea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                rows={6}
                aria-label="メール下書き"
                placeholder="「メールを下書き」で AI が案件・次アクションを踏まえた下書きを生成します。"
                className="w-full rounded-rw border border-line bg-surface p-2 text-sm focus:border-action focus:outline-none"
              />
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <OciButton onClick={() => void runDraft()} disabled={busy !== ''}>
                  {busy === 'draft' ? '… 実行中' : '🤖 メールを下書き'}
                </OciButton>
                <OciButton variant="outline" onClick={() => setSent(true)} disabled={!draft || sent}>
                  {sent ? '✓ 承認キューへ送信(デモ)' : '承認キューへ送信(デモ)'}
                </OciButton>
                <span className="text-[11px] text-ink-muted">実メールは送信しません</span>
              </div>
            </Panel>
          )}
        </div>

        {/* 右: 複合AI(議事録要約 → 次アクション) */}
        <div className="space-y-4">
          {err && (
            <p className="rounded-rw border border-line bg-surface px-3 py-2 text-xs text-primary-strong">
              ⚠ {err}
            </p>
          )}

          {has('minutes') && (
            <Panel
              title="📝 議事録要約"
              action={
                <OciButton
                  variant="outline"
                  onClick={() => void runSummary()}
                  disabled={busy !== '' || !meeting}
                >
                  {busy === 'summary' ? '… 実行中' : 'AI で要約'}
                </OciButton>
              }
            >
              {summary ? (
                <p className="whitespace-pre-wrap text-sm">{summary}</p>
              ) : (
                <p className="text-xs text-ink-muted">商談議事録を構造化要約します。</p>
              )}
            </Panel>
          )}

          {has('agent') && (
            <Panel
              title="✅ 次アクション提案"
              action={
                <OciButton variant="outline" onClick={() => void runActions()} disabled={busy !== ''}>
                  {busy === 'actions' ? '… 実行中' : 'AI で提案'}
                </OciButton>
              }
            >
              {actions.length ? (
                <ul className="list-disc space-y-1 pl-5 text-sm">
                  {actions.map((a, i) => (
                    <li key={i}>{a}</li>
                  ))}
                </ul>
              ) : (
                <p className="text-xs text-ink-muted">
                  案件情報{summary ? '＋議事録要約' : ''}から次アクションを提案します。
                </p>
              )}
            </Panel>
          )}

          {has('nl2sql') && (
            <Panel
              title="📊 売上集計(NL2SQL)"
              action={
                <OciButton variant="outline" onClick={() => void runRollup()} disabled={busy !== ''}>
                  {busy === 'rollup' ? '… 実行中' : '担当者別売上'}
                </OciButton>
              }
            >
              {rollup && rollup.rows.length ? (
                <table className="w-full text-left text-xs">
                  <thead className="text-ink-muted">
                    <tr>
                      {rollup.columns.map((c) => (
                        <th key={c} className="py-0.5 pr-2">
                          {c}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {rollup.rows.slice(0, 5).map((r, i) => (
                      <tr key={i}>
                        {r.map((v, j) => (
                          <td key={j} className="py-0.5 pr-2">
                            {v}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <p className="text-xs text-ink-muted">
                  専用スキーマ JETUSE_SBA04 へ自然言語で売上を集計し、メール下書きの根拠に連動させます。
                </p>
              )}
            </Panel>
          )}
        </div>
      </div>
    </div>
  )
}

/* ---------------- 売上分析(NL2SQL を専用スキーマに照会) ---------------- */

function AnalyticsView({
  hasRollup,
  callSlot,
}: {
  hasRollup: boolean
  callSlot: (cap: string, input: string) => Promise<SlotResult>
}) {
  const [q, setQ] = useState(ROLLUP_PRESETS[0])
  const [result, setResult] = useState<SlotResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const runRollup = async (question: string) => {
    setBusy(true)
    setErr(null)
    try {
      const r = await callSlot('nl2sql', question)
      setResult(r)
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e))
      setResult(null)
    } finally {
      setBusy(false)
    }
  }

  if (!hasRollup) {
    return (
      <Panel title="📊 売上集計">
        <p className="text-sm text-ink-muted">この環境では売上集計(NL2SQL)は利用できません。</p>
      </Panel>
    )
  }

  const cols = result?.columns ?? []
  const rows = result?.rows ?? []

  return (
    <div className="space-y-4">
      <Panel title="📊 売上集計（自然言語 → SQL / 専用スキーマ JETUSE_SBA04）">
        <div className="flex flex-wrap items-center gap-2">
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            aria-label="集計したい内容を自然言語で入力"
            placeholder="例: 担当者別の売上合計を多い順に"
            className="min-w-[18rem] flex-1 rounded-rw border border-line bg-surface px-2 py-1 text-sm focus:border-action focus:outline-none"
          />
          <OciButton onClick={() => void runRollup(q)} disabled={busy || !q.trim()}>
            {busy ? '… 集計中' : '🤖 集計する'}
          </OciButton>
        </div>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {ROLLUP_PRESETS.map((p) => (
            <button
              key={p}
              type="button"
              onClick={() => {
                setQ(p)
                void runRollup(p)
              }}
              disabled={busy}
              className="rounded-full border border-line px-2.5 py-0.5 text-xs text-ink-muted hover:border-action hover:text-action"
            >
              {p}
            </button>
          ))}
        </div>
      </Panel>

      {err && (
        <p className="rounded-rw border border-line bg-surface px-3 py-2 text-xs text-primary-strong">
          ⚠ {err}
        </p>
      )}

      {result && (
        <Panel title="結果">
          {result.sql && (
            <pre className="mb-3 overflow-x-auto rounded-rw border border-line bg-surface p-2 text-[11px] text-ink-muted">
              {result.sql}
            </pre>
          )}
          {cols.length && rows.length ? (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-ink-muted">
                    {cols.map((c) => (
                      <th key={c} className="px-2 py-1 font-medium">
                        {c}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r, i) => (
                    <tr key={i} className="border-b border-line/50">
                      {r.map((cell, j) => (
                        <td key={j} className="px-2 py-1 whitespace-nowrap">
                          {cell}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="text-sm text-ink-muted">該当する結果がありません。</p>
          )}
          <p className="mt-2 text-[11px] text-ink-muted">
            {result.row_count ?? rows.length} 行{result.truncated ? '（上限まで表示）' : ''}
          </p>
        </Panel>
      )}
    </div>
  )
}
