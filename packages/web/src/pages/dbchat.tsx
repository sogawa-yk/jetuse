/** DBチャット(SQL-02): 質問→SQL生成→ユーザー確認・編集→読取専用実行→結果テーブル。
 *  CSVデータセット・結果テーブル/グラフ・NL2SQLストリームは pages/dbchat/ 配下に分割(review-validation.md §7)。 */
import { useEffect, useState } from 'react'
import { authHeaders, useUser } from '../auth'
import { PageContainer } from '../components/layout'
import { usePrefs } from '../prefs'
import { DatasetPanel } from './dbchat/DatasetPanel'
import { PreviewTable } from './dbchat/PreviewTable'
import { ResultTable } from './dbchat/ResultTable'
import { useDbChatStream, type Nl2SqlBackend, type Nl2SqlTarget } from './dbchat/useDbChatStream'
import type { Result, SchemaTable } from './dbchat/types'

const SAMPLE_QUESTIONS = [
  '2001年の販売チャネル別の売上合計を教えて',
  '顧客数が多い国の上位5件は？',
  '売上金額が最も多い商品カテゴリの上位3件は？',
  '2001年の月別売上推移を見せて',
  'プロモーション別の売上合計トップ5は？',
]

export default function DbChat() {
  const { t } = usePrefs()
  const user = useUser()
  const [question, setQuestion] = useState('')
  const [sql, setSql] = useState('')
  const [result, setResult] = useState<Result | null>(null)
  const [resultSeq, setResultSeq] = useState(0) // 新しい結果ごとに ResultTable を作り直しグラフをリセット
  const [executing, setExecuting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [schema, setSchema] = useState<SchemaTable[]>([])
  const [schemaOpen, setSchemaOpen] = useState(true)
  const [openTable, setOpenTable] = useState<string | null>(null)
  const [backend, setBackend] = useState<Nl2SqlBackend>('sql_search')
  // ENH-02: テーブル中身プレビュー(スキーマ表示用)
  const [preview, setPreview] = useState<{ table: string; data: Result } | null>(null)
  const [previewing, setPreviewing] = useState(false)
  // ENH-01: 対象データ(SHサンプル / 本人CSVデータセット)
  const [target, setTarget] = useState<Nl2SqlTarget>('sample')
  // feedback 20260620 #3: Select AI のモデル選択
  const [saiModels, setSaiModels] = useState<{ key: string; label: string }[]>([])
  const [saiModel, setSaiModel] = useState('')

  // onError は空文字でクリア、それ以外はメッセージ表示(子コンポーネントと共通)
  const reportError = (msg: string) => setError(msg || null)

  const stream = useDbChatStream({
    user,
    t,
    onSql: setSql,
    onError: reportError,
  })

  const loadPreview = async (table: string) => {
    setPreviewing(true)
    setPreview(null)
    try {
      const res = await fetch(`/api/dbchat/preview?table=${encodeURIComponent(table)}`, {
        headers: authHeaders(user),
      })
      if (res.ok) setPreview({ table, data: await res.json() })
    } finally {
      setPreviewing(false)
    }
  }

  useEffect(() => {
    fetch('/api/dbchat/schema', { headers: authHeaders(user) })
      .then((r) => r.json())
      .then((d) => setSchema(d.tables ?? []))
      .catch(() => setSchema([]))
    // Select AI のモデル一覧(feedback 20260620 #3)
    fetch('/api/dbchat/select-ai-models', { headers: authHeaders(user) })
      .then((r) => r.json())
      .then((d) => {
        setSaiModels(d.models ?? [])
        setSaiModel((cur) => cur || d.default || '')
      })
      .catch(() => setSaiModels([]))
  }, [user])

  const generate = async () => {
    setError(null)
    setSql('')
    setResult(null)
    await stream.generate(question, backend, target, saiModel)
  }

  // Select AI のモデル選択を出す条件(データセット対象は常にSelect AI / サンプルはSelect AI選択時)
  const showSaiModel = (target === 'datasets' || backend === 'select_ai') && saiModels.length > 0

  const execute = async () => {
    if (!sql.trim() || executing) return
    setExecuting(true)
    setError(null)
    setResult(null)
    try {
      const res = await fetch('/api/dbchat/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
        body: JSON.stringify({ sql }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`)
      setResult(data)
      setResultSeq((n) => n + 1)
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e))
    } finally {
      setExecuting(false)
    }
  }

  return (
    <PageContainer wide icon="dbchat" title={t('nav.dbchat')} subtitle={t('db.lead')} helpKey="dbchat">
      <div className="space-y-4">
        {/* 対象データの切替(ENH-01): SHサンプル / 本人CSVデータセット */}
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-ink-muted">{t('db.target')}:</span>
          {(['sample', 'datasets'] as const).map((tg) => (
            <button
              key={tg}
              onClick={() => setTarget(tg)}
              className={`rounded-full px-3 py-1 text-xs ${
                target === tg ? 'bg-action-soft font-medium text-ink' : 'border border-line hover:border-action'
              }`}
            >
              {tg === 'sample' ? t('db.target.sample') : t('db.target.datasets')}
            </button>
          ))}
        </div>

        {/* マイデータ(CSVアップロード→DBチャット対象化 — ENH-01) */}
        {target === 'datasets' && <DatasetPanel onError={reportError} model={saiModel} />}

        {/* 質問できるデータ(スキーマ表示 — SQL-02b)。SHサンプル選択時のみ */}
        {target === 'sample' && (
        <div className="rounded-rw border border-line bg-surface">
          <button
            onClick={() => setSchemaOpen(!schemaOpen)}
            className="flex w-full items-center justify-between px-4 py-3 text-left text-sm font-semibold"
          >
            <span>📋 {t('db.schema.title')}</span>
            <span className="text-xs text-ink-muted">{schemaOpen ? '▲' : '▼'}</span>
          </button>
          {schemaOpen && (
            <div className="border-t border-line px-4 py-3">
              <p className="mb-2 text-xs text-ink-muted">{t('db.schema.lead')}</p>
              <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2 lg:grid-cols-3">
                {schema.map((tb) => (
                  <div key={tb.name} className="rounded-rw border border-line bg-bg">
                    <button
                      onClick={() => setOpenTable(openTable === tb.name ? null : tb.name)}
                      className="w-full px-2.5 py-1.5 text-left"
                    >
                      <div className="flex items-baseline justify-between gap-2">
                        <span className="font-mono text-xs font-semibold">{tb.name}</span>
                        {tb.rows != null && (
                          <span className="shrink-0 text-[10px] text-ink-muted">
                            {tb.rows.toLocaleString()}
                            {t('db.rows')}
                          </span>
                        )}
                      </div>
                      <div className="mt-0.5 text-[11px] leading-snug text-ink-muted">
                        {tb.comment}
                      </div>
                    </button>
                    {openTable === tb.name && (
                      <div className="border-t border-line px-2.5 py-1.5">
                        <ul className="text-[11px]">
                          {tb.columns.map((c) => (
                            <li key={c.name} className="flex gap-2">
                              <span className="font-mono">{c.name}</span>
                              <span className="text-ink-muted/70">{c.type}</span>
                              {c.comment && <span className="text-ink-muted">{c.comment}</span>}
                            </li>
                          ))}
                        </ul>
                        <button
                          onClick={() => void loadPreview(tb.name)}
                          disabled={previewing}
                          className="mt-1.5 rounded-rw border border-line px-2 py-0.5 text-[11px] text-action hover:border-action disabled:opacity-40"
                        >
                          {previewing && preview?.table !== tb.name ? '…' : t('db.preview')}
                        </button>
                        {preview?.table === tb.name && <PreviewTable data={preview.data} />}
                      </div>
                    )}
                  </div>
                ))}
              </div>
              <div className="mt-3 flex flex-wrap items-center gap-1.5">
                <span className="text-xs text-ink-muted">{t('db.samples')}:</span>
                {SAMPLE_QUESTIONS.map((q) => (
                  <button
                    key={q}
                    onClick={() => setQuestion(q)}
                    className="rounded-full border border-line px-2.5 py-1 text-xs hover:border-action hover:text-action"
                  >
                    {q}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
        )}

        {/* 質問 */}
        <div className="rounded-rw border border-line bg-surface p-4">
          <form
            className="flex flex-wrap items-end gap-2"
            onSubmit={(e) => {
              e.preventDefault()
              void generate()
            }}
          >
            <textarea
              rows={2}
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder={t('db.placeholder')}
              className="min-w-0 flex-1 resize-y rounded-rw border border-line bg-bg px-3 py-2 text-sm outline-none focus:border-action"
            />
            <select
              value={backend}
              onChange={(e) => setBackend(e.target.value as Nl2SqlBackend)}
              disabled={stream.generating}
              className="rounded-rw border border-line bg-bg px-2 py-2 text-xs outline-none focus:border-action"
              aria-label="nl2sql backend"
            >
              <option value="sql_search">{t('db.backend.ss')}</option>
              <option value="select_ai">{t('db.backend.sai')}</option>
            </select>
            {showSaiModel && (
              <select
                value={saiModel}
                onChange={(e) => setSaiModel(e.target.value)}
                disabled={stream.generating}
                className="rounded-rw border border-line bg-bg px-2 py-2 text-xs outline-none focus:border-action"
                aria-label={t('db.model')}
                title={t('db.model')}
              >
                {saiModels.map((m) => (
                  <option key={m.key} value={m.key}>{m.label}</option>
                ))}
              </select>
            )}
            {stream.generating ? (
              <button
                type="button"
                onClick={() => stream.stop()}
                className="rounded-rw border border-line px-4 py-2 text-sm text-ink-muted hover:border-action"
              >
                ■ {t('chat.stop')}
              </button>
            ) : (
              <button
                type="submit"
                disabled={!question.trim()}
                className="rounded-rw bg-cta px-4 py-2 text-sm font-medium text-cta-ink hover:bg-cta-strong disabled:opacity-40"
              >
                {t('db.generate')}
              </button>
            )}
          </form>
          {stream.generating && (
            <p className="mt-2 text-xs text-ink-muted">
              ⏳ {t('db.generating')} ({stream.elapsed}s / {t('db.generating.note')})
            </p>
          )}
        </div>

        {/* 生成SQL(確認・編集) */}
        {(sql || stream.generating) && (
          <div className="rounded-rw border border-line bg-surface p-4">
            <div className="mb-2 flex items-center justify-between">
              <h2 className="text-sm font-semibold text-ink-muted">{t('db.sql')}</h2>
              {sql && (
                <span className="flex gap-2">
                  <button
                    onClick={() => navigator.clipboard.writeText(sql)}
                    className="text-xs text-ink-muted hover:text-action"
                  >
                    ⧉ {t('chat.copy')}
                  </button>
                  <button
                    onClick={() => void execute()}
                    disabled={executing || !sql.trim()}
                    className="rounded-rw bg-cta px-3 py-1 text-xs font-medium text-cta-ink hover:bg-cta-strong disabled:opacity-40"
                  >
                    {executing ? t('db.executing') : `▶ ${t('db.execute')}`}
                  </button>
                </span>
              )}
            </div>
            <textarea
              rows={Math.min(14, Math.max(4, sql.split('\n').length + 1))}
              value={sql}
              onChange={(e) => setSql(e.target.value)}
              spellCheck={false}
              className="w-full resize-y rounded-rw border border-line bg-bg p-3 font-mono text-xs leading-relaxed outline-none focus:border-action"
            />
            <p className="mt-1 text-[11px] text-ink-muted">{t('db.guard')}</p>
          </div>
        )}

        {error && (
          <div className="rounded-rw border border-primary bg-primary-soft px-3 py-2 text-sm">
            ⚠ {error}
          </div>
        )}

        {/* 結果テーブル + グラフ(新しい結果ごとに作り直してグラフ状態をリセット) */}
        {result && (
          <ResultTable key={resultSeq} result={result} question={question} onError={reportError} />
        )}
      </div>
    </PageContainer>
  )
}
