/** RAGチャット(RAG-01/02): ファイル管理 + file_search付きチャット + 引用元表示 */
import { useEffect, useRef, useState } from 'react'
import { authHeaders, reauthenticate, useUser } from '../auth'
import { PageContainer } from '../components/layout'
import { Md } from '../components/markdown'
import { readSse } from '../lib/sse'
import { usePrefs } from '../prefs'

type BackendStatus = 'indexed' | 'pending' | 'error' | 'disabled'
type RagFile = {
  id: string
  filename: string
  status: 'processing' | 'completed' | 'failed'
  bytes?: number
  error?: string | null
  backends?: { vector_store: BackendStatus; select_ai: BackendStatus; opensearch: BackendStatus }
}
// 取り込み状況バッジの色
const beBadge: Record<BackendStatus, string> = {
  indexed: 'bg-pill-ok text-pill-ok-ink',
  pending: 'bg-band-chip/20 text-ink-muted',
  error: 'bg-primary-soft text-primary-strong',
  disabled: 'bg-band-chip/10 text-ink-muted/50',
}
const BACKEND_LABELS: { key: 'vector_store' | 'select_ai' | 'opensearch'; short: string }[] = [
  { key: 'vector_store', short: 'VS' },
  { key: 'select_ai', short: 'SAI' },
  { key: 'opensearch', short: 'OS' },
]
type Citation = { file_id: string; filename: string; score: number | null }
type Msg = { role: 'user' | 'assistant'; content: string; citations?: Citation[] }

const statusBadge: Record<RagFile['status'], string> = {
  processing: 'bg-band-chip/20 text-ink-muted',
  completed: 'bg-pill-ok text-pill-ok-ink',
  failed: 'bg-primary-soft text-primary-strong',
}

export default function Rag() {
  const { t } = usePrefs()
  const user = useUser()
  const [files, setFiles] = useState<RagFile[]>([])
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [backend, setBackend] = useState<'vector_store' | 'select_ai' | 'opensearch'>('vector_store')
  const abortRef = useRef<AbortController | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  const loadFiles = () =>
    fetch('/api/rag/files', { headers: authHeaders(user) })
      .then((r) => r.json())
      .then((d) => setFiles(d.files))
      .catch(() => setFiles([]))

  useEffect(() => {
    void loadFiles()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user])

  // 取り込み中(VS処理中 or いずれかのバックエンドがpending)は定期的に状態を更新。
  // Select AIは最大60分の同期間隔があるため、未反映の間はゆっくり(20秒)ポーリング
  useEffect(() => {
    const vsProcessing = files.some((f) => f.status === 'processing')
    const bePending = files.some((f) =>
      f.backends && Object.values(f.backends).some((s) => s === 'pending'),
    )
    if (!vsProcessing && !bePending) return
    const timer = setInterval(loadFiles, vsProcessing ? 5000 : 20000)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [files])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [msgs])

  const upload = async (file: File) => {
    setUploading(true)
    setUploadError(null)
    try {
      const form = new FormData()
      form.append('file', file)
      const res = await fetch('/api/rag/files', {
        method: 'POST',
        headers: authHeaders(user),
        body: form,
      })
      if (!res.ok) {
        const d = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }))
        throw new Error(typeof d.detail === 'string' ? d.detail : `HTTP ${res.status}`)
      }
      void loadFiles()
    } catch (e) {
      setUploadError(String(e instanceof Error ? e.message : e))
    } finally {
      setUploading(false)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  const removeFile = async (id: string) => {
    await fetch(`/api/rag/files/${id}`, { method: 'DELETE', headers: authHeaders(user) })
    void loadFiles()
  }

  const send = async () => {
    const text = input.trim()
    if (!text || busy) return
    setInput('')
    setBusy(true)
    const history: Msg[] = [...msgs, { role: 'user', content: text }]
    setMsgs([...history, { role: 'assistant', content: '' }])
    const ac = new AbortController()
    abortRef.current = ac
    try {
      const res = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
        body: JSON.stringify({
          model: 'gpt-oss-120b', // file_searchはResponses系のみ(specs/09)
          messages: history.map((m) => ({ role: m.role, content: m.content })),
          rag: true,
          rag_backend: backend,
        }),
        signal: ac.signal,
      })
      if (res.status === 401) {
        reauthenticate()
        throw new Error('セッションの有効期限が切れました。再ログインします…')
      }
      if (!res.ok || !res.body) {
        const d = await res.json().catch(() => null)
        throw new Error(d?.detail ?? `HTTP ${res.status}`)
      }
      const patchLast = (patch: (m: Msg) => Msg) =>
        setMsgs((cur) => {
          const next = [...cur]
          const last = next[next.length - 1]
          if (last?.role === 'assistant') next[next.length - 1] = patch(last)
          return next
        })
      await readSse<{ delta?: string; error?: string; citations?: Citation[] }>(
        res,
        (ev) => {
          if (ev.delta) patchLast((m) => ({ ...m, content: m.content + ev.delta }))
          if (ev.citations) patchLast((m) => ({ ...m, citations: ev.citations }))
          if (ev.error) patchLast((m) => ({ ...m, content: `${m.content}\n\n> ⚠ ${ev.error}` }))
        },
        { signal: ac.signal },
      )
    } catch (e) {
      const aborted = e instanceof DOMException && e.name === 'AbortError'
      if (!aborted) {
        const msg = String(e instanceof Error ? e.message : e)
        setMsgs((cur) => {
          const next = [...cur]
          const last = next[next.length - 1]
          if (last?.role === 'assistant') {
            next[next.length - 1] = { ...last, content: `${last.content}\n\n> ⚠ ${msg}` }
          }
          return next
        })
      }
    } finally {
      setBusy(false)
      abortRef.current = null
    }
  }

  const ready = files.some((f) => f.status === 'completed')

  return (
    <PageContainer wide icon="rag" title={t('nav.rag')} subtitle={t('rag.lead')} helpKey="rag">
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-5">
        {/* ファイル管理(RAG-01) */}
        <div className="min-w-0 lg:col-span-2">
          <div className="rounded-rw border border-line bg-surface p-4">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-sm font-semibold text-ink-muted">{t('rag.files')}</h2>
              <label className="cursor-pointer rounded-rw border border-line px-3 py-1.5 text-sm hover:border-action hover:text-action">
                {uploading ? t('rag.uploading') : `＋ ${t('rag.upload')}`}
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".pdf,.txt,.md"
                  className="hidden"
                  disabled={uploading}
                  onChange={(e) => {
                    const f = e.target.files?.[0]
                    if (f) void upload(f)
                  }}
                />
              </label>
            </div>
            <p className="mb-2 text-xs text-ink-muted">{t('rag.supported')}</p>
            {uploadError && (
              <p className="mb-2 text-xs text-primary-strong">⚠ {uploadError}</p>
            )}
            {files.length === 0 ? (
              <p className="py-6 text-center text-sm text-ink-muted/60">{t('rag.empty')}</p>
            ) : (
              <ul className="space-y-1.5">
                {files.map((f) => (
                  <li
                    key={f.id}
                    className="group flex items-center gap-2 rounded-rw border border-line bg-bg px-2.5 py-1.5 text-sm"
                  >
                    <span className="min-w-0 flex-1 truncate" title={f.filename}>
                      📄 {f.filename}
                    </span>
                    {f.backends ? (
                      <span className="flex shrink-0 gap-1">
                        {BACKEND_LABELS.map((b) => {
                          const st = f.backends![b.key]
                          const mark =
                            st === 'indexed' ? '✓' : st === 'pending' ? '⏳'
                              : st === 'error' ? '!' : '–'
                          return (
                            <span
                              key={b.key}
                              className={`rounded-full px-1.5 py-0.5 text-[10px] ${beBadge[st]}`}
                              title={`${t(`rag.be.${b.key}`)}: ${t(`rag.bestatus.${st}`)}`}
                            >
                              {mark} {b.short}
                            </span>
                          )
                        })}
                      </span>
                    ) : (
                      <span
                        className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] ${statusBadge[f.status]}`}
                        title={f.error ?? ''}
                      >
                        {f.status === 'processing' && '⏳ '}
                        {t(`rag.status.${f.status}`)}
                      </span>
                    )}
                    <button
                      onClick={() => void removeFile(f.id)}
                      className="invisible shrink-0 px-1 text-xs text-ink-muted hover:text-primary-strong group-hover:visible"
                      aria-label="delete file"
                    >
                      ✕
                    </button>
                  </li>
                ))}
              </ul>
            )}
            {files.length > 0 && files.some((f) => f.backends) && (
              <p className="mt-2 text-[10px] leading-snug text-ink-muted">{t('rag.beLegend')}</p>
            )}
          </div>
        </div>

        {/* RAGチャット(RAG-02) */}
        <div className="flex min-w-0 flex-col rounded-rw border border-line bg-surface lg:col-span-3">
          <div className="min-h-72 flex-1 space-y-4 overflow-y-auto p-4" style={{ maxHeight: '60vh' }}>
            {msgs.length === 0 && (
              <p className="py-10 text-center text-sm text-ink-muted/60">
                {ready ? t('rag.chatHint') : t('rag.needFiles')}
              </p>
            )}
            {backend === 'select_ai' && (
              <p className="rounded-rw border border-line bg-bg px-3 py-1.5 text-[11px] text-ink-muted">
                ⓘ {t('rag.backend.saiNote')}
              </p>
            )}
            {msgs.map((m, i) =>
              m.role === 'user' ? (
                <div key={i} className="flex justify-end">
                  <div className="max-w-[85%] whitespace-pre-wrap rounded-rw rounded-tr-none bg-band px-4 py-2.5 text-sm text-band-ink">
                    {m.content}
                  </div>
                </div>
              ) : (
                <div key={i} className="min-w-0">
                  <div className="md text-sm leading-relaxed">
                    <Md>{m.content || '…'}</Md>
                  </div>
                  {m.citations && m.citations.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {m.citations.map((c) => (
                        <span
                          key={c.file_id}
                          className="rounded-full border border-line bg-bg px-2 py-0.5 text-[11px] text-ink-muted"
                          title={c.score != null ? `score ${c.score}` : ''}
                        >
                          📎 {c.filename}
                          {c.score != null && (
                            <span className="ml-1 opacity-60">{c.score}</span>
                          )}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              ),
            )}
            <div ref={bottomRef} />
          </div>
          <form
            className="flex items-end gap-2 border-t border-line p-3"
            onSubmit={(e) => {
              e.preventDefault()
              void send()
            }}
          >
            <select
              value={backend}
              onChange={(e) => setBackend(e.target.value as 'vector_store' | 'select_ai' | 'opensearch')}
              disabled={busy}
              className="rounded-rw border border-line bg-bg px-2 py-2 text-xs outline-none focus:border-action"
              aria-label="rag backend"
              title={backend === 'select_ai' ? t('rag.backend.saiNote') : ''}
            >
              <option value="vector_store">VS — {t('rag.backend.vs')}</option>
              <option value="select_ai">SAI — {t('rag.backend.sai')}</option>
              <option value="opensearch">OS — {t('rag.backend.os')}</option>
            </select>
            <textarea
              rows={1}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault()
                  void send()
                }
              }}
              placeholder={ready ? t('rag.placeholder') : t('rag.needFiles')}
              disabled={!ready}
              className="max-h-32 min-w-0 flex-1 resize-none rounded-rw border border-line bg-bg px-3 py-2 text-sm outline-none focus:border-action disabled:opacity-50"
            />
            {busy ? (
              <button
                type="button"
                onClick={() => abortRef.current?.abort()}
                className="rounded-rw border border-line px-4 py-2 text-sm font-medium text-ink-muted hover:border-action hover:text-action"
              >
                ■ {t('chat.stop')}
              </button>
            ) : (
              <button
                type="submit"
                disabled={!input.trim() || !ready}
                className="rounded-rw bg-cta px-4 py-2 text-sm font-medium text-cta-ink hover:bg-cta-strong disabled:opacity-40"
              >
                {t('chat.send')}
              </button>
            )}
          </form>
        </div>
      </div>
    </PageContainer>
  )
}
