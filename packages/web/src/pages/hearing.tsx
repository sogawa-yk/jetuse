/** スタンダードモード ダイアログ式ヒアリングUI(HBD-02)。
 *
 *  フィールドSAが「ヒアリングメモ貼付 → 順次Q&A(デフォルト提案つき) → 推薦構成の提示」までを
 *  画面で行う。各回答は HBD-01 のヒアリングAPIへ構造化保存する(回答=source:'sa'、提案=
 *  'genai_suggested')。推薦は決定ルール(recommend)で生成し、画面に提示する(ブラックボックス化しない)。
 *
 *  フロー(docs/enhance/202607-hearing-flow.md §2):
 *    入力(メモ) → GenAI が各質問のデフォルトを提案 → Q1..Q6 を順次回答(分岐表示・進捗 n/6)
 *    → 「確定」で推薦構成(主SBA＋AI部品＋コネクタ＋UI＋シード)を提示 → SA が確定。
 *
 *  本タスクは Q&A→推薦提示まで(構成生成・プレビューは HBD-03、合成バリデーションは HBD-04)。 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { authHeaders, reauthenticate, useUser } from '../auth'
import { PageContainer } from '../components/layout'
import { OciButton, Panel, StatusBadge } from '../components/oci'
import { usePrefs } from '../prefs'

type TKey = Parameters<ReturnType<typeof usePrefs>['t']>[0]
type T = (k: TKey) => string

type QOption = { id: string; label: string }
type Question = {
  id: string
  type: 'single' | 'multi' | 'auto'
  text: string
  purpose?: string
  options: QOption[]
  required: boolean
  min_selections: number
}
type AnswerValue = string | string[]
type Validation = { ok: boolean; missing_capabilities: string[]; warnings: string[] }
type Recommendation = {
  sample_app: string | null
  secondary_sample_apps: string[]
  ai_parts: string[]
  highlight: string | null
  connectors: string[]
  ui: string
  seed_strategy: string
  needs_genai_nearest: boolean
  rationale: string[]
  validation: Validation
  genai_nearest_sample_app?: string | null
  confirmed_at?: string | null
}
type StoredAnswer = { question_id: string; value: AnswerValue; source: string }
type Session = {
  id: string
  input_notes?: string | null
  answers?: StoredAnswer[]
  recommendation?: Recommendation | null
}

type Step = 'input' | 'qa' | 'result'

/** FastAPI のエラー `detail` を表示用文字列にする。422 は detail が配列(各要素 .msg)になる。 */
function detailMessage(detail: unknown, status: number): string {
  if (typeof detail === 'string' && detail) return detail
  if (Array.isArray(detail)) {
    const msgs = detail
      .map((d) => (d && typeof d === 'object' && 'msg' in d ? String((d as { msg: unknown }).msg) : ''))
      .filter(Boolean)
    if (msgs.length) return msgs.join('; ')
  }
  return `HTTP ${status}`
}

function isAnswered(q: Question, v: AnswerValue | undefined): boolean {
  if (v == null) return false
  if (q.type === 'multi') return Array.isArray(v) && v.length >= Math.max(1, q.min_selections)
  return typeof v === 'string' && v !== ''
}

/** 推薦素材の機械キー(capability/コネクタ/UI/シード)を SA 向け表示文言へ。未知キーはそのまま。 */
function partLabel(t: T, key: string): string {
  const k = `hearing.part.${key}` as TKey
  const label = t(k)
  return label === k ? key : label
}

export default function Hearing() {
  const { t } = usePrefs()
  const user = useUser()
  const [params, setParams] = useSearchParams()

  const [step, setStep] = useState<Step>('input')
  const [questions, setQuestions] = useState<Question[]>([])
  const [notes, setNotes] = useState('')
  const [session, setSession] = useState<Session | null>(null)
  const [answers, setAnswers] = useState<Record<string, AnswerValue>>({})
  // 現在 GenAI 提案のまま(SA 未修正)の質問 id。修正したら外す(提案バッジを消す)。
  const [suggested, setSuggested] = useState<Record<string, boolean>>({})
  const [qi, setQi] = useState(0)
  const [rec, setRec] = useState<Recommendation | null>(null)
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [confirmed, setConfirmed] = useState(false)
  // 進行中の回答保存(PUT)件数。>0 の間は遷移/推薦/選択変更をロックし、保存と
  // 画面遷移の競合(次へ済みなのに前問の PUT が後から失敗・ロールバック)を防ぐ。
  const [savingCount, setSavingCount] = useState(0)
  const isSaving = savingCount > 0

  const sid = params.get('sid')
  // 既に読み込んだ(または自分で作成した)セッション id。URL の sid 変化で再フェッチして
  // ローカルの回答を上書きしないためのガード(自作セッションの再開フェッチを抑止)。
  const loadedSidRef = useRef<string | null>(null)
  // 質問ごとの保存リクエスト世代。古い PUT の結果が新しい選択を上書きしないようにする。
  const saveSeqRef = useRef<Record<string, number>>({})
  // 現在のセッション id(保存中に sid 切替/リスタートが起きたら旧セッションの結果を捨てる)。
  const sessionIdRef = useRef<string | null>(null)

  const api = useCallback(
    async (path: string, init?: RequestInit): Promise<unknown> => {
      const res = await fetch(`/api/hearing${path}`, {
        ...init,
        headers: {
          ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
          ...authHeaders(user),
          ...init?.headers,
        },
      })
      if (res.status === 401) {
        reauthenticate()
        throw new Error(t('hearing.sessionLost'))
      }
      if (!res.ok) {
        const b = (await res.json().catch(() => null)) as { detail?: unknown } | null
        throw new Error(detailMessage(b?.detail, res.status))
      }
      return res.json()
    },
    [user, t],
  )

  // 現在のセッション id を ref に同期(保存中の stale な結果反映を弾く判定に使う)。
  useEffect(() => {
    sessionIdRef.current = session?.id ?? null
  }, [session])

  // 質問スキーマ(Q1..Q6 / auto は除外)を取得する。
  useEffect(() => {
    let cancelled = false
    api('/questions')
      .then((d) => {
        if (cancelled) return
        const qs = ((d as { questions?: Question[] }).questions ?? []).filter(
          (q) => q.type !== 'auto',
        )
        setQuestions(qs)
      })
      .catch((e: unknown) => {
        // 質問取得失敗は握りつぶさず表示する(空の QA で詰まらせない)。
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e))
      })
    return () => {
      cancelled = true
    }
  }, [api])

  // ?sid= 付き再開: 既存セッションの回答・推薦を復元する(途中離脱→再開)。
  useEffect(() => {
    // 自分で作成済み/読込済みの sid は再フェッチしない(回答の上書き防止)。
    if (!sid || loadedSidRef.current === sid) return
    let cancelled = false
    // 別 sid をロードする前に表示状態を初期化する。失敗(404/401/500)時に直前のセッション内容が
    // 残って古い session.id へ操作が飛ぶのを防ぐ(stale state 防止)。loadedSid も無効化し、
    // クリア後に元の sid へ戻ったら再取得できるようにする(成功時のみ下で再記録)。
    loadedSidRef.current = null
    setSession(null)
    setAnswers({})
    setSuggested({})
    setNotes('')
    setRec(null)
    setConfirmed(false)
    setStep('input')
    setErr(null)
    // sid は URL クエリ由来。`/` や `..` 等を含む不正値でパスを汚さないようエンコードする。
    api(`/sessions/${encodeURIComponent(sid)}`)
      .then((d) => {
        if (cancelled) return
        // 取得成功後に「読込済み」を記録する。一時障害(401/500)で失敗したら
        // 未読込のままにし、再認証/再レンダーで同じ sid を再試行できるようにする。
        loadedSidRef.current = sid
        const s = d as Session
        setSession(s)
        setNotes(s.input_notes ?? '')
        const restored: Record<string, AnswerValue> = {}
        const sg: Record<string, boolean> = {}
        for (const a of s.answers ?? []) {
          restored[a.question_id] = a.value
          if (a.source === 'genai_suggested') sg[a.question_id] = true
        }
        setAnswers(restored)
        setSuggested(sg)
        if (s.recommendation) {
          setRec(s.recommendation)
          setConfirmed(Boolean(s.recommendation.confirmed_at))
          setStep('result')
        } else {
          setStep('qa')
        }
      })
      .catch((e: unknown) => {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e))
      })
    return () => {
      cancelled = true
    }
  }, [sid, api])

  const answerable = questions
  const total = answerable.length
  const answeredCount = answerable.filter((q) => isAnswered(q, answers[q.id])).length

  // メモから開始: セッション作成 → GenAI 提案 → 各質問のデフォルトを埋めて Q&A へ。
  const start = async (withSuggest: boolean) => {
    setBusy('start')
    setErr(null)
    try {
      const s = (await api('/sessions', {
        method: 'POST',
        body: JSON.stringify({ input_notes: notes || null }),
      })) as Session
      setSession(s)
      // 自作セッションは再開フェッチ不要(下の useEffect が上書きしないようガード)。
      loadedSidRef.current = s.id
      // 再開できるよう URL に sid を載せる(リロード耐性)。
      setParams({ sid: s.id }, { replace: true })
      if (withSuggest && notes.trim()) {
        const sug = (await api(`/sessions/${s.id}/suggest`, {
          method: 'POST',
          body: JSON.stringify({ save: true }),
        })) as { suggestions?: Record<string, AnswerValue> }
        const map = sug.suggestions ?? {}
        setAnswers(map)
        const sg: Record<string, boolean> = {}
        for (const k of Object.keys(map)) sg[k] = true
        setSuggested(sg)
      }
      setQi(0)
      setStep('qa')
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy('')
    }
  }

  // 回答を保存(手入力 → source='sa')。楽観更新するが、PUT が失敗したら必ず巻き戻す
  // (未保存の回答で進捗が進み・推薦/再開が不整合になるのを防ぐ。採点指摘 review-1 blocker)。
  const saveAnswer = async (q: Question, value: AnswerValue) => {
    const prevValue = answers[q.id]
    const prevSuggested = Boolean(suggested[q.id])
    // 楽観更新(選択を即時反映)＋提案バッジ解除。
    setAnswers((cur) => ({ ...cur, [q.id]: value }))
    setSuggested((cur) => {
      if (!cur[q.id]) return cur
      const next = { ...cur }
      delete next[q.id]
      return next
    })
    if (!session) return
    const sid0 = session.id
    // 世代番号: この保存が最新かを後で判定する(古い PUT の結果で新しい選択を壊さない)。
    const mySeq = (saveSeqRef.current[q.id] ?? 0) + 1
    saveSeqRef.current[q.id] = mySeq
    setErr(null)
    setSavingCount((c) => c + 1)
    try {
      await api(`/sessions/${sid0}/answers/${q.id}`, {
        method: 'PUT',
        body: JSON.stringify({ value }),
      })
    } catch (e) {
      // 保存中に sid 切替/リスタートでセッションが変わったら、旧セッションの結果は捨てる。
      if (sessionIdRef.current !== sid0) return
      // 後続の保存に追い越されていたら、古い失敗で UI を巻き戻さない。
      if (saveSeqRef.current[q.id] !== mySeq) return
      // 保存失敗 → 直前状態へロールバック(この質問は未保存=未回答のまま扱う)。
      setAnswers((cur) => {
        const next = { ...cur }
        if (prevValue === undefined) delete next[q.id]
        else next[q.id] = prevValue
        return next
      })
      if (prevSuggested) setSuggested((cur) => ({ ...cur, [q.id]: true }))
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setSavingCount((c) => c - 1)
    }
  }

  const selectSingle = (q: Question, optId: string) => void saveAnswer(q, optId)
  const toggleMulti = (q: Question, optId: string) => {
    const cur = Array.isArray(answers[q.id]) ? (answers[q.id] as string[]) : []
    const next = cur.includes(optId) ? cur.filter((x) => x !== optId) : [...cur, optId]
    void saveAnswer(q, next)
  }

  // SA が「確認」した回答を sa として確定保存する。GenAI 提案(genai_suggested)を SA が
  // そのまま採用した質問も、PUT で source='sa' へ昇格させる(要求: SA は確認・修正)。
  const confirmAnswerAsSa = async (q: Question): Promise<boolean> => {
    if (!session || !isAnswered(q, answers[q.id])) return true
    const sid0 = session.id
    const mySeq = (saveSeqRef.current[q.id] ?? 0) + 1
    saveSeqRef.current[q.id] = mySeq
    setSavingCount((c) => c + 1)
    try {
      await api(`/sessions/${sid0}/answers/${q.id}`, {
        method: 'PUT',
        body: JSON.stringify({ value: answers[q.id] }),
      })
      // セッションが切り替わっていたら旧セッションの結果は反映しない。
      if (sessionIdRef.current !== sid0) return false
      setSuggested((cur) => {
        if (!cur[q.id]) return cur
        const next = { ...cur }
        delete next[q.id]
        return next
      })
      return true
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
      return false
    } finally {
      setSavingCount((c) => c - 1)
    }
  }

  // 「次へ」: 現在の質問を SA 確定として保存してから進む(未修正の提案も sa に昇格)。
  const goNext = async (q: Question, nextIndex: number) => {
    if (await confirmAnswerAsSa(q)) setQi(nextIndex)
  }

  // 推薦生成: 全回答を sa として確定保存 → 決定ルールへ。422(未回答/不正)はエラー表示。
  const recommend = async () => {
    if (!session) return
    setBusy('recommend')
    setErr(null)
    try {
      // 提案そのまま採用を含め、全回答を sa として確定保存してから推薦する。
      for (const q of answerable) {
        if (isAnswered(q, answers[q.id])) {
          if (!(await confirmAnswerAsSa(q))) {
            setBusy('')
            return
          }
        }
      }
      const r = (await api(`/sessions/${session.id}/recommend`, { method: 'POST' })) as Recommendation
      setRec(r)
      setConfirmed(false)
      setStep('result')
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy('')
    }
  }

  const confirm = async () => {
    if (!session) return
    setBusy('confirm')
    setErr(null)
    try {
      await api(`/sessions/${session.id}/recommend/confirm`, { method: 'POST' })
      setConfirmed(true)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy('')
    }
  }

  // 推薦結果から Q&A へ戻って回答を修正する。確定状態・旧推薦は無効化し、
  // 再度「確定して推薦」させる(古い推薦が confirmed のまま残らないようにする)。
  const editAnswers = () => {
    setConfirmed(false)
    setRec(null)
    setStep('qa')
  }

  const restart = () => {
    setStep('input')
    setSession(null)
    setAnswers({})
    setSuggested({})
    setRec(null)
    setConfirmed(false)
    setQi(0)
    setErr(null)
    // 読込済みガードを解除(同じ ?sid= に戻ったときに再開フェッチが効くように)。
    loadedSidRef.current = null
    saveSeqRef.current = {}
    setParams({}, { replace: true })
  }

  // Q2 に業務DB かつ Q3=集計分析 → SBA-B(NL2SQL)を主役に格上げ(§3 分岐例)。Q3 画面で予告。
  const branchToSbaB = useMemo(() => {
    const q2 = answers.Q2
    return Array.isArray(q2) && q2.includes('business_db') && answers.Q3 === 'nl2sql'
  }, [answers])

  return (
    <PageContainer
      wide
      icon="checklist"
      title={t('hearing.title')}
      subtitle={t('hearing.subtitle')}
    >
      {err && (
        <p className="mb-4 rounded-rw border border-line bg-surface px-3 py-2 text-sm text-primary-strong">
          ⚠ {err}
        </p>
      )}

      {step === 'input' && (
        <InputStep
          t={t}
          notes={notes}
          setNotes={setNotes}
          busy={busy}
          onStart={start}
        />
      )}

      {step === 'qa' && total > 0 && (
        <QaStep
          t={t}
          questions={answerable}
          qi={qi}
          setQi={setQi}
          answers={answers}
          suggested={suggested}
          answeredCount={answeredCount}
          total={total}
          branchToSbaB={branchToSbaB}
          busy={busy}
          saving={isSaving}
          onSelectSingle={selectSingle}
          onToggleMulti={toggleMulti}
          onNext={goNext}
          onRecommend={recommend}
        />
      )}

      {step === 'result' && rec && (
        <ResultStep
          t={t}
          rec={rec}
          confirmed={confirmed}
          busy={busy}
          onConfirm={confirm}
          onRestart={restart}
          onBack={editAnswers}
        />
      )}
    </PageContainer>
  )
}

/* ---------------- 入力ステップ(メモ貼付 → 提案) ---------------- */

function InputStep({
  t,
  notes,
  setNotes,
  busy,
  onStart,
}: {
  t: T
  notes: string
  setNotes: (v: string) => void
  busy: string
  onStart: (withSuggest: boolean) => void
}) {
  return (
    <Panel title={t('hearing.notes.title')}>
      <p className="mb-2 text-sm text-ink-muted">{t('hearing.notes.hint')}</p>
      <textarea
        value={notes}
        onChange={(e) => setNotes(e.target.value)}
        rows={8}
        aria-label={t('hearing.notes.title')}
        placeholder={t('hearing.notes.placeholder')}
        className="w-full rounded-rw border border-line bg-surface p-3 text-sm focus:border-action focus:outline-none"
      />
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <OciButton onClick={() => onStart(true)} disabled={busy !== '' || !notes.trim()}>
          {busy === 'start' ? `… ${t('hearing.starting')}` : `🤖 ${t('hearing.suggestStart')}`}
        </OciButton>
        <OciButton variant="outline" onClick={() => onStart(false)} disabled={busy !== ''}>
          {t('hearing.skipStart')}
        </OciButton>
      </div>
    </Panel>
  )
}

/* ---------------- Q&A ステップ(順次・分岐・進捗) ---------------- */

function QaStep({
  t,
  questions,
  qi,
  setQi,
  answers,
  suggested,
  answeredCount,
  total,
  branchToSbaB,
  busy,
  saving,
  onSelectSingle,
  onToggleMulti,
  onNext,
  onRecommend,
}: {
  t: T
  questions: Question[]
  qi: number
  setQi: (n: number) => void
  answers: Record<string, AnswerValue>
  suggested: Record<string, boolean>
  answeredCount: number
  total: number
  branchToSbaB: boolean
  busy: string
  saving: boolean
  onSelectSingle: (q: Question, optId: string) => void
  onToggleMulti: (q: Question, optId: string) => void
  onNext: (q: Question, nextIndex: number) => void
  onRecommend: () => void
}) {
  const q = questions[qi]
  if (!q) return null
  const value = answers[q.id]
  const isLast = qi === total - 1
  const answered = isAnswered(q, value)
  const showBranch = q.id === 'Q3' && branchToSbaB
  // 保存中/推薦中は遷移・選択変更をロック(保存と画面遷移の競合を断つ)。
  const locked = saving || busy === 'recommend'
  // 推薦の前提は「必須質問がすべて回答済み」(件数ではなく required で判定)。
  const requiredDone = questions.every((it) => !it.required || isAnswered(it, answers[it.id]))

  const isSelected = (optId: string): boolean =>
    q.type === 'multi'
      ? Array.isArray(value) && value.includes(optId)
      : value === optId

  return (
    <div className="space-y-4">
      {/* 進捗(n/total)＋ステップドット */}
      <div className="flex items-center gap-3">
        <div className="text-sm font-medium text-ink-muted" aria-label={t('hearing.progress')}>
          {answeredCount}/{total}
        </div>
        <div className="flex flex-1 items-center gap-1.5">
          {questions.map((it, i) => (
            <button
              key={it.id}
              type="button"
              aria-label={it.id}
              aria-current={i === qi ? 'step' : undefined}
              disabled={locked}
              onClick={() => setQi(i)}
              className={`h-2 flex-1 rounded-full transition-colors ${
                i === qi
                  ? 'bg-action'
                  : isAnswered(it, answers[it.id])
                    ? 'bg-band'
                    : 'bg-line'
              }`}
            />
          ))}
        </div>
      </div>

      <Panel
        title={
          <span className="flex flex-wrap items-center gap-2">
            <span className="rounded-full bg-action-soft px-2 py-0.5 text-xs text-ink">{q.id}</span>
            <span>{q.text}</span>
            {suggested[q.id] && (
              <span className="rounded-full bg-band/10 px-2 py-0.5 text-[10px] font-medium text-band">
                🤖 {t('hearing.suggestBadge')}
              </span>
            )}
          </span>
        }
      >
        {q.purpose && <p className="mb-2 text-xs text-ink-muted">{q.purpose}</p>}
        {q.type === 'multi' && (
          <p className="mb-2 text-xs text-ink-muted">{t('hearing.multiHint')}</p>
        )}
        <div className="space-y-2">
          {q.options.map((opt) => {
            const sel = isSelected(opt.id)
            return (
              <button
                key={opt.id}
                type="button"
                role={q.type === 'multi' ? 'checkbox' : 'radio'}
                aria-checked={sel}
                // 保存中/推薦確定中は選択変更を止め、保存と PUT の競合を防ぐ。
                disabled={locked}
                onClick={() =>
                  q.type === 'multi' ? onToggleMulti(q, opt.id) : onSelectSingle(q, opt.id)
                }
                className={`flex w-full items-center gap-3 rounded-rw border px-3 py-2.5 text-left text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${
                  sel
                    ? 'border-action bg-action-soft font-medium text-ink'
                    : 'border-line bg-surface text-ink hover:border-action'
                }`}
              >
                <span
                  aria-hidden
                  className={`flex h-4 w-4 shrink-0 items-center justify-center border text-[10px] ${
                    q.type === 'multi' ? 'rounded-sm' : 'rounded-full'
                  } ${sel ? 'border-action bg-action text-white' : 'border-line'}`}
                >
                  {sel ? '✓' : ''}
                </span>
                {opt.label}
              </button>
            )
          })}
        </div>

        {showBranch && (
          <p className="mt-3 rounded-rw border border-band/40 bg-band/5 px-3 py-2 text-xs text-band">
            ⤴ {t('hearing.branch.sbaB')}
          </p>
        )}
      </Panel>

      <div className="flex items-center justify-between gap-2">
        <OciButton variant="outline" onClick={() => setQi(qi - 1)} disabled={qi === 0 || locked}>
          ← {t('hearing.back')}
        </OciButton>
        {isLast ? (
          <OciButton
            onClick={onRecommend}
            disabled={busy !== '' || locked || !requiredDone}
          >
            {busy === 'recommend' ? `… ${t('hearing.recommending')}` : t('hearing.confirm')}
          </OciButton>
        ) : (
          <OciButton onClick={() => onNext(q, qi + 1)} disabled={locked || (!answered && q.required)}>
            {t('hearing.next')} →
          </OciButton>
        )}
      </div>
    </div>
  )
}

/* ---------------- 推薦提示ステップ(ブラックボックス化しない) ---------------- */

function ResultStep({
  t,
  rec,
  confirmed,
  busy,
  onConfirm,
  onRestart,
  onBack,
}: {
  t: T
  rec: Recommendation
  confirmed: boolean
  busy: string
  onConfirm: () => void
  onRestart: () => void
  onBack: () => void
}) {
  const chips = (items: string[]) =>
    items.length ? (
      <div className="flex flex-wrap gap-1.5">
        {items.map((x) => (
          <span key={x} className="rounded-full bg-action-soft px-2.5 py-0.5 text-xs text-ink">
            {partLabel(t, x)}
          </span>
        ))}
      </div>
    ) : (
      <span className="text-sm text-ink-muted">{t('hearing.result.none')}</span>
    )

  return (
    <div className="space-y-4">
      <Panel title={t('hearing.result.title')}>
        <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div>
            <dt className="text-xs font-medium text-ink-muted">{t('hearing.result.mainSba')}</dt>
            <dd className="mt-1 flex items-center gap-2">
              {rec.sample_app ? (
                <StatusBadge kind="ok">{rec.sample_app}</StatusBadge>
              ) : (
                <span className="text-sm text-ink-muted">
                  {rec.genai_nearest_sample_app
                    ? `${t('hearing.result.nearestAdvice')}: ${rec.genai_nearest_sample_app}`
                    : t('hearing.result.unresolved')}
                </span>
              )}
            </dd>
          </div>
          <div>
            <dt className="text-xs font-medium text-ink-muted">{t('hearing.result.highlight')}</dt>
            <dd className="mt-1 text-sm">
              {rec.highlight ? partLabel(t, rec.highlight) : t('hearing.result.none')}
            </dd>
          </div>
          <div className="sm:col-span-2">
            <dt className="mb-1 text-xs font-medium text-ink-muted">{t('hearing.result.aiParts')}</dt>
            <dd>{chips(rec.ai_parts)}</dd>
          </div>
          <div>
            <dt className="mb-1 text-xs font-medium text-ink-muted">{t('hearing.result.connectors')}</dt>
            <dd>{chips(rec.connectors)}</dd>
          </div>
          <div>
            <dt className="text-xs font-medium text-ink-muted">{t('hearing.result.ui')}</dt>
            <dd className="mt-1 text-sm">{partLabel(t, rec.ui)}</dd>
          </div>
          <div>
            <dt className="text-xs font-medium text-ink-muted">{t('hearing.result.seed')}</dt>
            <dd className="mt-1 text-sm">{partLabel(t, rec.seed_strategy)}</dd>
          </div>
        </dl>

        {(rec.validation.missing_capabilities.length > 0 || rec.validation.warnings.length > 0) && (
          <div className="mt-4 space-y-1 rounded-rw border border-pill-warn/40 bg-pill-warn/10 px-3 py-2">
            <p className="text-xs font-medium text-pill-warn-ink">⚠ {t('hearing.result.warnings')}</p>
            {rec.validation.missing_capabilities.map((m) => (
              <p key={m} className="text-[11px] text-pill-warn-ink">
                · {t('hearing.result.missing')}: {partLabel(t, m)}
              </p>
            ))}
            {rec.validation.warnings.map((w) => (
              <p key={w} className="text-[11px] text-pill-warn-ink">
                · {w}
              </p>
            ))}
          </div>
        )}
      </Panel>

      {/* 決定ルールの根拠(監査・ブラックボックス化を避ける) */}
      <Panel title={t('hearing.result.rationale')}>
        <ul className="list-disc space-y-1 pl-5 text-xs text-ink-muted">
          {rec.rationale.map((r, i) => (
            <li key={i}>{r}</li>
          ))}
        </ul>
      </Panel>

      {/* 主SBA未確定(Q1=その他)は API が confirm を 409 で拒否する。確定を無効化し、
          最近傍提案を参考に Q1 を具体化するよう誘導する(行き止まりにしない)。 */}
      {!rec.sample_app && (
        <p className="rounded-rw border border-pill-warn/40 bg-pill-warn/10 px-3 py-2 text-xs text-pill-warn-ink">
          ⚠ {t('hearing.result.needNearest')}
        </p>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <OciButton variant="outline" onClick={onBack}>
          ← {t('hearing.result.editAnswers')}
        </OciButton>
        {confirmed ? (
          <StatusBadge kind="ok">✓ {t('hearing.result.confirmed')}</StatusBadge>
        ) : (
          <OciButton onClick={onConfirm} disabled={busy !== '' || !rec.sample_app}>
            {busy === 'confirm' ? '…' : t('hearing.result.confirm')}
          </OciButton>
        )}
        <OciButton variant="ghost" onClick={onRestart}>
          {t('hearing.result.restart')}
        </OciButton>
      </div>
    </div>
  )
}
