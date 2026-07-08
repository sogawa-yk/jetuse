/** デモビルダー(SP3-05 / specs/19 §7): ヒアリング → プラン確認 → 生成 → プレビュー → 確定の
 *  1画面ウィザード。プラン JSON は直接編集させない(§7② — 修正は追加発話 → 再設計。
 *  タイトル/説明のみ直接編集 = PATCH /plan)。直近セッションの復帰は localStorage(§7)。 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { authHeaders, useUser, type User } from '../../auth'
import { PageContainer } from '../../components/layout'
import { usePrefs } from '../../prefs'
import {
  checklist, clearSid, deriveStep, loadSid, saveSid,
  type Demo, type MessageOut, type Plan, type Session, type Step,
} from './state'

/** API エラー(detail 付き)。FastAPI の 409/422 detail をそのまま通知に出す */
class ApiError extends Error {
  status: number
  constructor(status: number, detail: string) {
    super(detail)
    this.status = status
  }
}

async function api<T>(user: User, path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...authHeaders(user), ...init?.headers },
  })
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const body = (await res.json()) as { detail?: unknown }
      if (body.detail) {
        detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
      }
    } catch {
      /* JSON でないエラー応答はステータスのまま */
    }
    throw new ApiError(res.status, detail)
  }
  return res.json() as Promise<T>
}

const errMsg = (e: unknown): string => (e instanceof Error ? e.message : String(e))

const POLL_MS = 3000

export default function DemoBuilder() {
  const { t } = usePrefs()
  const user = useUser()

  const [session, setSession] = useState<Session | null>(null)
  const [demo, setDemo] = useState<Demo | null>(null)
  const [step, setStep] = useState<Step>(1)
  const [restoring, setRestoring] = useState(() => !!loadSid())
  const [gone, setGone] = useState(false) // デモが外部で削除された等の終端状態
  const [done, setDone] = useState(false) // 確定完了
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [input, setInput] = useState('')
  // 直近ヒアリング応答の判定(復帰時は決定的チェックリストから導出。最終判定はサーバ — §2.3)
  const [sufficient, setSufficient] = useState(false)
  const [missing, setMissing] = useState<string[]>([])
  // §7②: タイトル/説明のみ直接編集(プランへ反映は生成開始時に PATCH /plan)
  const [title, setTitle] = useState('')
  const [desc, setDesc] = useState('')
  // §7⑤: 確定フォーム(PATCH /api/demos/{id})
  const [confirmName, setConfirmName] = useState('')
  const [confirmDesc, setConfirmDesc] = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)

  const call = useCallback(
    <T,>(path: string, init?: RequestInit) => api<T>(user, path, init),
    [user],
  )

  const adoptPlan = useCallback((p: Plan | null | undefined) => {
    setTitle(p?.title ?? '')
    setDesc(p?.description ?? '')
  }, [])

  /** demo の反映。ready なら確定フォーム(⑤)へ name/description をプリフィル */
  const adoptDemo = useCallback((d: Demo) => {
    setDemo(d)
    if (d.status === 'ready') {
      setConfirmName(d.name)
      setConfirmDesc(d.description ?? '')
    }
  }, [])

  // 直近セッションの復帰(localStorage — §7)。404 は新規開始へフォールバック
  useEffect(() => {
    const sid = loadSid()
    if (!sid) return
    call<Session>(`/api/builder/sessions/${sid}`)
      .then(async (s) => {
        setSession(s)
        adoptPlan(s.plan)
        setSufficient(checklist(s.requirements).every((c) => c.ok))
        if (s.demo_id) {
          try {
            adoptDemo(await call<Demo>(`/api/demos/${s.demo_id}`))
          } catch {
            setGone(true) // デモ行が消えている(存在秘匿 404 含む)
          }
        }
        setStep(deriveStep(s))
      })
      .catch(() => clearSid())
      .finally(() => setRestoring(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps -- マウント時に1回だけ復帰する
  }, [])

  // 生成進行のポーリング(§7③ — 202 後は GET /api/demos/{id} の status を見る)
  const failed = demo?.status === 'failed'
  useEffect(() => {
    const id = session?.demo_id
    if (step !== 3 || !id || failed || gone) return
    let stopped = false
    const tick = async () => {
      try {
        const d = await call<Demo>(`/api/demos/${id}`)
        if (stopped) return
        adoptDemo(d)
        if (d.status === 'ready') setStep(4)
      } catch (e) {
        if (!stopped && e instanceof ApiError && e.status === 404) setGone(true)
      }
    }
    void tick()
    const timer = setInterval(() => void tick(), POLL_MS)
    return () => {
      stopped = true
      clearInterval(timer)
    }
  }, [step, session?.demo_id, failed, gone, call, adoptDemo])

  // ヒアリング吹き出しの末尾へ自動スクロール
  useEffect(() => {
    bottomRef.current?.scrollIntoView?.({ behavior: 'smooth' })
  }, [session?.transcript.length])

  const reset = () => {
    clearSid()
    setSession(null)
    setDemo(null)
    setStep(1)
    setGone(false)
    setDone(false)
    setNotice(null)
    setInput('')
    setSufficient(false)
    setMissing([])
    adoptPlan(null)
    setConfirmName('')
    setConfirmDesc('')
  }

  /** §7①: NL 発話(POST messages)。初回はセッション作成から */
  const send = async () => {
    const content = input.trim()
    if (!content || busy) return
    setBusy(true)
    setNotice(null)
    try {
      let s = session
      if (!s) {
        s = await call<Session>('/api/builder/sessions', { method: 'POST' })
        saveSid(s.id)
      }
      const r = await call<MessageOut>(`/api/builder/sessions/${s.id}/messages`, {
        method: 'POST',
        body: JSON.stringify({ content }),
      })
      setInput('')
      setSufficient(r.sufficient)
      setMissing(r.missing)
      setSession({
        ...s,
        requirements: r.requirements,
        transcript: [
          ...s.transcript,
          { role: 'user', content },
          { role: 'assistant', content: r.reply },
        ],
      })
    } catch (e) {
      setNotice(errMsg(e))
    } finally {
      setBusy(false)
    }
  }

  /** §7②: デモ設計(POST design)。designed 後の再実行 = プラン上書き(§3.1) */
  const runDesign = async () => {
    if (!session || busy) return
    setBusy(true)
    setNotice(null)
    try {
      const s = await call<Session>(`/api/builder/sessions/${session.id}/design`, {
        method: 'POST',
      })
      setSession(s)
      adoptPlan(s.plan)
      setStep(2)
    } catch (e) {
      setNotice(errMsg(e))
    } finally {
      setBusy(false)
    }
  }

  /** §7③: 生成開始(POST generate → 202)。failed からの再生成も同じ経路(§4.5)。
   *  タイトル/説明が編集されていれば先にプランへ反映(PATCH /plan — サーバで再検証) */
  const generate = async () => {
    if (!session || busy) return
    setBusy(true)
    setNotice(null)
    try {
      const p = session.plan
      if (p && !session.demo_id && (title.trim() !== p.title || desc.trim() !== p.description)) {
        const s2 = await call<Session>(`/api/builder/sessions/${session.id}/plan`, {
          method: 'PATCH',
          body: JSON.stringify({ title: title.trim(), description: desc.trim() }),
        })
        setSession(s2)
      }
      const r = await call<{ demo_id: string }>(
        `/api/builder/sessions/${session.id}/generate`,
        { method: 'POST' },
      )
      setSession((cur) =>
        cur ? { ...cur, demo_id: r.demo_id, demo_status: 'provisioning' } : cur,
      )
      setDemo(null) // ポーリング再開(failed ガード解除)
      setStep(3)
    } catch (e) {
      setNotice(errMsg(e))
    } finally {
      setBusy(false)
    }
  }

  /** §7④: プレビュー。一回性コード(app-session — ADR-0023 §3.5)を添えて /app/ を新タブで開く。
   *  発行不能(AUTH オフのプレビュー = 秘密鍵未設定で fail-closed)は素の GET で通るため
   *  プレーン URL にフォールバックする */
  const openPreview = async () => {
    if (!session?.demo_id) return
    setNotice(null)
    const base = `/api/demos/${session.demo_id}/app/`
    let url = base
    try {
      const r = await call<{ code: string }>(`/api/demos/${session.demo_id}/app-session`, {
        method: 'POST',
      })
      url = `${base}?c=${encodeURIComponent(r.code)}`
    } catch {
      /* AUTH オフ配備では /app/ が dev-user で通る(AUTH=true 全経路は descope — SP3-05 引継) */
    }
    window.open(url, '_blank', 'noopener')
  }

  /** §7⑤: 確定(PATCH name/description — SP2 CRUD)。確定後は demos 一覧に現れる */
  const confirm = async () => {
    if (!session?.demo_id || busy || !confirmName.trim()) return
    setBusy(true)
    setNotice(null)
    try {
      await call<Demo>(`/api/demos/${session.demo_id}`, {
        method: 'PATCH',
        body: JSON.stringify({
          name: confirmName.trim(),
          description: confirmDesc.trim() || null,
        }),
      })
      clearSid()
      setDone(true)
    } catch (e) {
      setNotice(errMsg(e))
    } finally {
      setBusy(false)
    }
  }

  /** §7⑤: 破棄(DELETE — 確認ダイアログ付き。specs/18 の後始末が走る) */
  const discard = async () => {
    if (!session?.demo_id || busy) return
    if (!window.confirm(t('demobuilder.discard.confirm'))) return
    setBusy(true)
    setNotice(null)
    try {
      await call(`/api/demos/${session.demo_id}`, { method: 'DELETE' })
      reset()
    } catch (e) {
      setNotice(errMsg(e))
    } finally {
      setBusy(false)
    }
  }

  const items = checklist(session?.requirements)
  const deterministicOk = items.every((c) => c.ok)

  return (
    <PageContainer
      icon="idea"
      title={t('demobuilder.title')}
      subtitle={t('demobuilder.lead')}
      wide
    >
      <Stepper step={step} />
      {notice && (
        <div className="mb-4 flex items-start justify-between gap-2 rounded-rw border border-primary bg-primary-soft px-3 py-2 text-sm">
          <span className="whitespace-pre-wrap">⚠ {notice}</span>
          <button onClick={() => setNotice(null)} aria-label="dismiss" className="px-1">
            ✕
          </button>
        </div>
      )}
      {restoring ? (
        <p className="py-8 text-center text-sm text-ink-muted">{t('demobuilder.restoring')}</p>
      ) : done ? (
        <DoneView onNew={reset} />
      ) : gone ? (
        <div className="space-y-3 py-8 text-center text-sm">
          <p>{t('demobuilder.gone')}</p>
          <button onClick={reset} className="rounded-rw bg-cta px-4 py-2 text-cta-ink hover:bg-cta-strong">
            {t('demobuilder.new')}
          </button>
        </div>
      ) : step === 1 ? (
        <div className="grid gap-4 md:grid-cols-3">
          <div className="md:col-span-2 flex min-h-[420px] flex-col rounded-rw border border-line bg-surface">
            <div className="flex-1 space-y-3 overflow-y-auto p-4">
              {!session || session.transcript.length === 0 ? (
                <div className="flex h-full items-center justify-center">
                  <div className="text-center">
                    <div className="mb-2 text-3xl">🧑‍💼</div>
                    <p className="font-medium">{t('demobuilder.hearing.empty.title')}</p>
                    <p className="mt-1 text-sm text-ink-muted">
                      {t('demobuilder.hearing.empty.body')}
                    </p>
                  </div>
                </div>
              ) : (
                session.transcript.map((m, i) =>
                  m.role === 'user' ? (
                    <div key={i} className="flex justify-end">
                      <div className="max-w-[80%] whitespace-pre-wrap rounded-rw rounded-tr-none bg-band px-4 py-2.5 text-sm text-band-ink">
                        {m.content}
                      </div>
                    </div>
                  ) : (
                    <div key={i} className="flex justify-start gap-2">
                      <span className="mt-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-band-chip text-xs font-bold text-white">
                        AI
                      </span>
                      <div className="max-w-[85%] whitespace-pre-wrap rounded-rw rounded-tl-none border border-line bg-surface px-4 py-2.5 text-sm leading-relaxed">
                        {m.content}
                      </div>
                    </div>
                  ),
                )
              )}
              {busy && <p className="text-xs text-ink-muted">…</p>}
              <div ref={bottomRef} />
            </div>
            <form
              className="flex items-end gap-2 border-t border-line p-3"
              onSubmit={(e) => {
                e.preventDefault()
                void send()
              }}
            >
              <textarea
                rows={2}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                    e.preventDefault()
                    void send()
                  }
                }}
                placeholder={t('demobuilder.hearing.placeholder')}
                className="max-h-40 min-h-[3.5rem] min-w-0 flex-1 resize-none rounded-rw border border-line bg-surface px-3 py-2 text-sm leading-relaxed outline-none focus:border-action"
              />
              <button
                type="submit"
                disabled={!input.trim() || busy}
                className="shrink-0 rounded-rw bg-cta px-4 py-2.5 text-sm font-medium text-cta-ink hover:bg-cta-strong disabled:cursor-not-allowed disabled:opacity-40"
              >
                {t('demobuilder.send')}
              </button>
            </form>
          </div>
          {/* 必須項目の充足可視化(§7① — §2.2 の必須フィールド) */}
          <aside className="space-y-3 rounded-rw border border-line bg-surface p-4 text-sm">
            <p className="font-medium">{t('demobuilder.reqs.title')}</p>
            <ul className="space-y-1.5">
              {items.map((c) => (
                <li key={c.key} className="flex items-center gap-2">
                  <span
                    className={`flex h-5 w-5 items-center justify-center rounded-full text-[11px] ${
                      c.ok ? 'bg-action-soft text-ink' : 'border border-line text-ink-muted'
                    }`}
                  >
                    {c.ok ? '✓' : '–'}
                  </span>
                  {t(`demobuilder.reqs.${c.key}`)}
                </li>
              ))}
            </ul>
            <p
              className={`rounded-rw px-2.5 py-1.5 text-xs ${
                sufficient ? 'bg-action-soft' : 'bg-bg text-ink-muted'
              }`}
            >
              {sufficient
                ? t('demobuilder.reqs.sufficient')
                : t('demobuilder.reqs.insufficient')}
            </p>
            {missing.length > 0 && (
              <p className="text-xs text-ink-muted">
                {t('demobuilder.reqs.missing')} {missing.join(' / ')}
              </p>
            )}
            <button
              onClick={() => void runDesign()}
              disabled={busy || !session || !(sufficient || deterministicOk)}
              className="w-full rounded-rw bg-cta px-4 py-2 text-sm font-medium text-cta-ink hover:bg-cta-strong disabled:cursor-not-allowed disabled:opacity-40"
            >
              {busy ? t('demobuilder.designing') : t('demobuilder.toDesign')}
            </button>
          </aside>
        </div>
      ) : step === 2 && session?.plan ? (
        <PlanReview
          plan={session.plan}
          title={title}
          desc={desc}
          busy={busy}
          onTitle={setTitle}
          onDesc={setDesc}
          onBack={() => setStep(1)}
          onGenerate={() => void generate()}
        />
      ) : step === 3 ? (
        <div className="space-y-4 py-8 text-center text-sm">
          {failed ? (
            <>
              <p className="font-medium text-primary-strong">✕ {t('demobuilder.failed')}</p>
              {demo?.config?.generation?.error && (
                <p className="mx-auto max-w-2xl whitespace-pre-wrap rounded-rw border border-line bg-surface px-4 py-3 text-left text-xs text-ink-muted">
                  {demo.config.generation.error}
                </p>
              )}
              <button
                onClick={() => void generate()}
                disabled={busy}
                className="rounded-rw bg-cta px-5 py-2.5 text-sm font-medium text-cta-ink hover:bg-cta-strong disabled:opacity-40"
              >
                ↻ {t('demobuilder.regenerate')}
              </button>
            </>
          ) : (
            <>
              <div className="mx-auto h-8 w-8 animate-spin rounded-full border-2 border-line border-t-accent" />
              <p>{t('demobuilder.generating')}</p>
              {demo?.config?.generation?.step && (
                <p className="text-xs text-ink-muted">{demo.config.generation.step}</p>
              )}
            </>
          )}
        </div>
      ) : step === 4 ? (
        <div className="space-y-4 py-8 text-center text-sm">
          <div className="text-3xl">🎉</div>
          <p className="font-medium">{t('demobuilder.preview.lead')}</p>
          <div className="flex flex-wrap items-center justify-center gap-2">
            <button
              onClick={() => void openPreview()}
              className="rounded-rw bg-cta px-5 py-2.5 text-sm font-medium text-cta-ink hover:bg-cta-strong"
            >
              {t('demobuilder.preview.open')} ↗
            </button>
            <button
              onClick={() => setStep(5)}
              className="rounded-rw border border-line px-5 py-2.5 text-sm font-medium hover:border-action hover:text-action"
            >
              {t('demobuilder.toConfirm')} →
            </button>
          </div>
          <button
            onClick={() => void discard()}
            disabled={busy}
            className="text-xs text-ink-muted underline hover:text-primary-strong"
          >
            {t('demobuilder.discard')}
          </button>
        </div>
      ) : (
        <div className="mx-auto max-w-xl space-y-4">
          <label className="block text-sm">
            <span className="font-medium">{t('demobuilder.confirm.name')}</span>
            <input
              value={confirmName}
              onChange={(e) => setConfirmName(e.target.value)}
              maxLength={200}
              className="mt-1 w-full rounded-rw border border-line bg-surface px-3 py-2 text-sm outline-none focus:border-action"
            />
          </label>
          <label className="block text-sm">
            <span className="font-medium">{t('demobuilder.confirm.desc')}</span>
            <textarea
              rows={3}
              value={confirmDesc}
              onChange={(e) => setConfirmDesc(e.target.value)}
              maxLength={1000}
              className="mt-1 w-full resize-y rounded-rw border border-line bg-surface px-3 py-2 text-sm outline-none focus:border-action"
            />
          </label>
          <div className="flex flex-wrap items-center gap-2">
            <button
              onClick={() => void confirm()}
              disabled={busy || !confirmName.trim()}
              className="rounded-rw bg-cta px-5 py-2.5 text-sm font-medium text-cta-ink hover:bg-cta-strong disabled:cursor-not-allowed disabled:opacity-40"
            >
              {t('demobuilder.confirm.save')}
            </button>
            <button
              onClick={() => setStep(4)}
              className="rounded-rw border border-line px-4 py-2.5 text-sm hover:border-action hover:text-action"
            >
              ← {t('demobuilder.back')}
            </button>
            <button
              onClick={() => void discard()}
              disabled={busy}
              className="ml-auto text-xs text-ink-muted underline hover:text-primary-strong"
            >
              {t('demobuilder.discard')}
            </button>
          </div>
        </div>
      )}
    </PageContainer>
  )
}

/** ウィザードの進行表示(①〜⑤)。クリック遷移はさせない(遷移はサーバ状態が正) */
function Stepper({ step }: { step: Step }) {
  const { t } = usePrefs()
  const labels = [
    t('demobuilder.step1'),
    t('demobuilder.step2'),
    t('demobuilder.step3'),
    t('demobuilder.step4'),
    t('demobuilder.step5'),
  ]
  return (
    <ol className="mb-5 flex flex-wrap items-center gap-1.5 text-xs">
      {labels.map((label, i) => {
        const n = (i + 1) as Step
        return (
          <li key={n} className="flex items-center gap-1.5">
            {i > 0 && <span className="text-line">—</span>}
            <span
              aria-current={n === step ? 'step' : undefined}
              className={`flex items-center gap-1.5 rounded-full px-2.5 py-1 ${
                n === step
                  ? 'bg-action-soft font-semibold text-ink'
                  : n < step
                    ? 'text-ink'
                    : 'text-ink-muted'
              }`}
            >
              <span
                className={`flex h-4 w-4 items-center justify-center rounded-full text-[10px] ${
                  n < step ? 'bg-band-chip text-white' : 'border border-line'
                }`}
              >
                {n < step ? '✓' : n}
              </span>
              {label}
            </span>
          </li>
        )
      })}
    </ol>
  )
}

/** §7②: プランの要約表示。JSON は編集させない — タイトル/説明のみ入力欄 */
function PlanReview({
  plan, title, desc, busy, onTitle, onDesc, onBack, onGenerate,
}: {
  plan: Plan
  title: string
  desc: string
  busy: boolean
  onTitle: (v: string) => void
  onDesc: (v: string) => void
  onBack: () => void
  onGenerate: () => void
}) {
  const { t } = usePrefs()
  const tables = plan.data.tables ?? []
  const documents = plan.data.documents ?? []
  return (
    <div className="space-y-4">
      <div className="grid gap-3 md:grid-cols-2">
        <label className="block text-sm">
          <span className="font-medium">{t('demobuilder.plan.title')}</span>
          <input
            value={title}
            onChange={(e) => onTitle(e.target.value)}
            maxLength={200}
            className="mt-1 w-full rounded-rw border border-line bg-surface px-3 py-2 text-sm outline-none focus:border-action"
          />
        </label>
        <label className="block text-sm">
          <span className="font-medium">{t('demobuilder.plan.description')}</span>
          <textarea
            rows={2}
            value={desc}
            onChange={(e) => onDesc(e.target.value)}
            maxLength={1000}
            className="mt-1 w-full resize-y rounded-rw border border-line bg-surface px-3 py-2 text-sm outline-none focus:border-action"
          />
        </label>
      </div>
      <div className="rounded-rw border border-line bg-surface p-4 text-sm">
        <p className="font-medium">{t('demobuilder.plan.capabilities')}</p>
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {plan.capabilities.map((c) => (
            <span key={c} className="rounded-full bg-action-soft px-2.5 py-0.5 text-xs">
              {c}
            </span>
          ))}
        </div>
        <p className="mt-3 font-medium">{t('demobuilder.plan.screens')}</p>
        <ul className="mt-1.5 space-y-2">
          {plan.screens.map((s) => (
            <li key={s.id} className="rounded-rw border border-line bg-bg px-3 py-2">
              <p className="font-medium">{s.title}</p>
              {s.description && <p className="text-xs text-ink-muted">{s.description}</p>}
              <ul className="mt-1 flex flex-wrap gap-1.5">
                {s.blocks.map((b, i) => (
                  <li key={i} className="rounded-full border border-line px-2 py-0.5 text-xs">
                    {b.title}
                    <span className="ml-1 text-ink-muted">({b.type})</span>
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
        {tables.length > 0 && (
          <>
            <p className="mt-3 font-medium">{t('demobuilder.plan.tables')}</p>
            <ul className="mt-1.5 space-y-1 text-xs">
              {tables.map((tb) => (
                <li key={tb.name}>
                  <b>{tb.title}</b>
                  <span className="text-ink-muted">
                    {' '}
                    ({tb.name} / {tb.rows} rows / {tb.columns.length} cols)
                  </span>
                </li>
              ))}
            </ul>
          </>
        )}
        {documents.length > 0 && (
          <>
            <p className="mt-3 font-medium">{t('demobuilder.plan.documents')}</p>
            <ul className="mt-1.5 space-y-1 text-xs">
              {documents.map((d) => (
                <li key={d.filename}>
                  <b>{d.title}</b>
                  <span className="text-ink-muted"> ({d.filename})</span>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
      <p className="text-xs text-ink-muted">{t('demobuilder.plan.editHint')}</p>
      <div className="flex flex-wrap gap-2">
        <button
          onClick={onBack}
          className="rounded-rw border border-line px-4 py-2.5 text-sm hover:border-action hover:text-action"
        >
          ← {t('demobuilder.backToHearing')}
        </button>
        <button
          onClick={onGenerate}
          disabled={busy || !title.trim() || !desc.trim()}
          className="rounded-rw bg-cta px-5 py-2.5 text-sm font-medium text-cta-ink hover:bg-cta-strong disabled:cursor-not-allowed disabled:opacity-40"
        >
          {t('demobuilder.generate')} →
        </button>
      </div>
    </div>
  )
}

/** §7⑤ 確定完了: 一覧(SP2 CRUD)に現れたことの案内 */
function DoneView({ onNew }: { onNew: () => void }) {
  const { t } = usePrefs()
  return (
    <div className="space-y-3 py-10 text-center text-sm">
      <div className="text-3xl">✅</div>
      <p className="font-medium">{t('demobuilder.done.title')}</p>
      <p className="text-ink-muted">{t('demobuilder.done.body')}</p>
      <button
        onClick={onNew}
        className="rounded-rw bg-cta px-4 py-2 text-sm font-medium text-cta-ink hover:bg-cta-strong"
      >
        {t('demobuilder.new')}
      </button>
    </div>
  )
}
