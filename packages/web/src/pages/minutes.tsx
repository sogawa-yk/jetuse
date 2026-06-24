/** 議事録生成(VOICE-01): 音声アップロード→バッチ文字起こし(話者分離)→LLM整形 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { authHeaders, reauthenticate, useUser } from '../auth'
import { PageContainer } from '../components/layout'
import { Md } from '../components/markdown'
import { readSse } from '../lib/sse'
import { OciButton, Panel, StatusBadge } from '../components/oci'
import { usePrefs } from '../prefs'

type JobSummary = { id: string; title: string; status: string; created_at: string }
type Utterance = { speaker: number; start: number; end: number; text: string }
type JobDetail = JobSummary & {
  speaker_count?: number | null
  transcript?: Utterance[] | null
  error?: string | null
}

const STATUS_KIND = {
  processing: 'warn',
  completed: 'ok',
  failed: 'err',
} as const

// 話者チップの色(順番に割当。tokens.cssのパレット系ユーティリティを使用)
const SPEAKER_STYLES = [
  'bg-action-soft text-ink',
  'bg-pill-ok text-pill-ok-ink',
  'bg-pill-warn text-pill-warn-ink',
  'bg-band/10 text-ink',
  'bg-pill-err text-pill-err-ink',
]

const fmtTime = (sec: number) => {
  const m = Math.floor(sec / 60)
  const s = Math.floor(sec % 60)
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

export default function Minutes() {
  const { t } = usePrefs()
  const user = useUser()
  const [jobs, setJobs] = useState<JobSummary[]>([])
  const [selected, setSelected] = useState<JobDetail | null>(null)
  const [language, setLanguage] = useState('ja')
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [template, setTemplate] = useState<'minutes' | 'faq' | 'article'>('minutes')
  const [output, setOutput] = useState('')
  const [generating, setGenerating] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const abortRef = useRef<AbortController | null>(null)

  const loadJobs = useCallback(() => {
    fetch('/api/minutes', { headers: authHeaders(user) })
      .then((r) => r.json())
      .then((d) => setJobs(d.jobs ?? []))
      .catch(() => setJobs([]))
  }, [user])

  useEffect(loadJobs, [loadJobs])

  const select = useCallback(
    async (id: string) => {
      setOutput('')
      try {
        const res = await fetch(`/api/minutes/${id}`, { headers: authHeaders(user) })
        if (res.status === 401) return reauthenticate()
        const data = await res.json()
        if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`)
        setSelected(data)
        // 一覧側のバッジも即時同期(ポーリング停止後に「処理中」が残る競合の防止)
        setJobs((prev) =>
          prev.map((j) => (j.id === data.id ? { ...j, status: data.status } : j)),
        )
      } catch (e) {
        setError(String(e instanceof Error ? e.message : e))
      }
    },
    [user],
  )

  // 処理中ジョブを選択している間は5秒ポーリング(specs/12)
  useEffect(() => {
    if (!selected || selected.status !== 'processing') return
    const timer = setInterval(() => {
      select(selected.id)
      loadJobs()
    }, 5000)
    return () => clearInterval(timer)
  }, [selected, select, loadJobs])

  const upload = async () => {
    const file = fileRef.current?.files?.[0]
    if (!file || uploading) return
    setUploading(true)
    setError(null)
    try {
      const form = new FormData()
      form.append('file', file)
      const res = await fetch(`/api/minutes?language=${encodeURIComponent(language)}`, {
        method: 'POST',
        headers: authHeaders(user),
        body: form,
      })
      if (res.status === 401) return reauthenticate()
      const data = await res.json()
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`)
      if (fileRef.current) fileRef.current.value = ''
      loadJobs()
      select(data.id)
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e))
    } finally {
      setUploading(false)
    }
  }

  const remove = async (id: string) => {
    if (!confirm(t('minutes.deleteConfirm'))) return
    await fetch(`/api/minutes/${id}`, { method: 'DELETE', headers: authHeaders(user) })
    if (selected?.id === id) setSelected(null)
    loadJobs()
  }

  const generate = async () => {
    if (!selected || generating) return
    setGenerating(true)
    setOutput('')
    setError(null)
    const ac = new AbortController()
    abortRef.current = ac
    try {
      const res = await fetch(`/api/minutes/${selected.id}/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
        body: JSON.stringify({ template }),
        signal: ac.signal,
      })
      if (res.status === 401) {
        reauthenticate()
        throw new Error(t('uc.sessionLost'))
      }
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)
      await readSse<{ delta?: string; error?: string }>(
        res,
        (ev) => {
          if (ev.delta) setOutput((o) => o + ev.delta)
          if (ev.error) setError(ev.error)
        },
        { signal: ac.signal },
      )
    } catch (e) {
      const aborted = e instanceof DOMException && e.name === 'AbortError'
      if (!aborted) setError(String(e instanceof Error ? e.message : e))
    } finally {
      setGenerating(false)
      abortRef.current = null
    }
  }

  return (
    <PageContainer icon="minutes" title={t('nav.minutes')} subtitle={t('minutes.lead')} wide helpKey="minutes">
      {error && (
        <div className="mb-4 rounded-rw bg-pill-err px-3 py-2 text-sm text-pill-err-ink">
          {error}
        </div>
      )}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        {/* 左: アップロード + ジョブ一覧 */}
        <div className="space-y-4">
          <Panel title={t('minutes.upload.title')}>
            <div className="space-y-3 text-sm">
              <input
                ref={fileRef}
                type="file"
                accept=".mp3,.wav,.m4a,.ogg,.webm,audio/*"
                className="block w-full text-xs file:mr-3 file:rounded-rw file:border-0 file:bg-cta file:px-3 file:py-1.5 file:text-xs file:font-medium file:text-cta-ink"
              />
              <div className="flex items-center gap-2">
                <label className="text-xs text-ink-muted">{t('minutes.language')}</label>
                <select
                  value={language}
                  onChange={(e) => setLanguage(e.target.value)}
                  className="rounded-rw border border-line bg-surface px-2 py-1 text-xs"
                >
                  <option value="ja">日本語</option>
                  <option value="en">English</option>
                </select>
                <OciButton onClick={upload} disabled={uploading} className="ml-auto">
                  {uploading ? t('minutes.uploading') : t('rag.upload')}
                </OciButton>
              </div>
              <p className="text-[11px] text-ink-muted">{t('minutes.supported')}</p>
            </div>
          </Panel>
          <Panel title={t('minutes.jobs')}>
            {jobs.length === 0 ? (
              <p className="text-xs text-ink-muted">{t('minutes.empty')}</p>
            ) : (
              <ul className="space-y-1">
                {jobs.map((j) => (
                  <li key={j.id}>
                    <button
                      onClick={() => select(j.id)}
                      className={`flex w-full items-center gap-2 rounded-rw px-2 py-1.5 text-left text-sm ${
                        selected?.id === j.id ? 'bg-action-soft' : 'hover:bg-bg'
                      }`}
                    >
                      <span className="min-w-0 flex-1 truncate">{j.title}</span>
                      <StatusBadge kind={STATUS_KIND[j.status as keyof typeof STATUS_KIND] ?? 'neutral'}>
                        {t(`minutes.status.${j.status}` as Parameters<typeof t>[0])}
                      </StatusBadge>
                      <span
                        role="button"
                        title={t('uc.deleteConfirm')}
                        onClick={(e) => {
                          e.stopPropagation()
                          remove(j.id)
                        }}
                        className="px-1 text-ink-muted hover:text-action"
                      >
                        ✕
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Panel>
        </div>

        {/* 右: トランスクリプト + 整形 */}
        <div className="space-y-4 lg:col-span-2">
          <Panel
            title={t('minutes.transcript')}
            action={
              selected?.status === 'completed' ? (
                <span className="text-xs text-ink-muted">
                  {t('minutes.speakers')}: {selected.speaker_count ?? '-'}
                </span>
              ) : undefined
            }
          >
            {!selected ? (
              <p className="text-xs text-ink-muted">{t('minutes.selectHint')}</p>
            ) : selected.status === 'processing' ? (
              <p className="text-sm text-ink-muted">{t('minutes.processing')}</p>
            ) : selected.status === 'failed' ? (
              <p className="text-sm text-pill-err-ink">{selected.error}</p>
            ) : (
              <div className="max-h-96 space-y-2 overflow-y-auto pr-1">
                {(selected.transcript ?? []).map((u, i) => (
                  <div key={i} className="flex items-start gap-2 text-sm">
                    <span className="mt-0.5 w-12 shrink-0 text-right text-[11px] tabular-nums text-ink-muted">
                      {fmtTime(u.start)}
                    </span>
                    <span
                      className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium ${SPEAKER_STYLES[u.speaker % SPEAKER_STYLES.length]}`}
                    >
                      {t('minutes.speaker')}
                      {u.speaker + 1}
                    </span>
                    <span className="min-w-0">{u.text}</span>
                  </div>
                ))}
              </div>
            )}
          </Panel>

          <Panel
            title={t('minutes.generate')}
            action={
              <div className="flex items-center gap-2 text-xs">
                <select
                  value={template}
                  onChange={(e) => setTemplate(e.target.value as typeof template)}
                  className="rounded-rw border border-line bg-surface px-2 py-1"
                >
                  <option value="minutes">{t('minutes.tpl.minutes')}</option>
                  <option value="faq">{t('minutes.tpl.faq')}</option>
                  <option value="article">{t('minutes.tpl.article')}</option>
                </select>
                {generating ? (
                  <OciButton variant="outline" onClick={() => abortRef.current?.abort()}>
                    {t('chat.stop')}
                  </OciButton>
                ) : (
                  <OciButton onClick={generate} disabled={selected?.status !== 'completed'}>
                    {t('uc.run')}
                  </OciButton>
                )}
                {output && !generating && (
                  <OciButton variant="ghost" onClick={() => navigator.clipboard.writeText(output)}>
                    {t('chat.copy')}
                  </OciButton>
                )}
              </div>
            }
          >
            {output ? (
              <div className="md text-sm">
                <Md>{output}</Md>
              </div>
            ) : (
              <p className="text-xs text-ink-muted">
                {generating ? t('minutes.generating') : t('minutes.generateHint')}
              </p>
            )}
          </Panel>
        </div>
      </div>
    </PageContainer>
  )
}
