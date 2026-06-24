/** ユースケース実行ページ(UC-01)。スキーマ→フォーム自動生成→/api/chat/streamで実行 */
import { useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { authHeaders, reauthenticate, useUser } from '../auth'
import { PageContainer } from '../components/layout'
import { Md } from '../components/markdown'
import { readSse } from '../lib/sse'
import {
  initialValues,
  missingRequired,
  renderTemplate,
  UcForm,
  type UcDefinition,
} from '../components/ucform'
import { usePrefs } from '../prefs'

export default function UsecaseRun() {
  const { id } = useParams()
  const { t } = usePrefs()
  const user = useUser()
  const [def, setDef] = useState<UcDefinition | null>(null)
  const [notFound, setNotFound] = useState(false)
  const [values, setValues] = useState<Record<string, string>>({})
  const [models, setModels] = useState<{ key: string; label: string }[]>([])
  const [model, setModel] = useState('gpt-oss-120b')
  const [output, setOutput] = useState('')
  const [busy, setBusy] = useState(false)
  const [phase, setPhase] = useState<'idle' | 'fetching' | 'streaming'>('idle')
  const [error, setError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  useEffect(() => {
    if (!id) return
    fetch(`/api/usecases/${id}`, { headers: authHeaders(user) })
      .then((r) => {
        if (!r.ok) throw new Error(String(r.status))
        return r.json()
      })
      .then((d: UcDefinition) => {
        setDef(d)
        setValues(initialValues(d.fields))
        if (d.model) setModel(d.model)
      })
      .catch(() => setNotFound(true))
    fetch('/api/chat/models', { headers: authHeaders(user) })
      .then((r) => r.json())
      .then((d) => setModels(d.models))
      .catch(() => setModels([]))
  }, [id, user])

  const run = async () => {
    if (!def || busy) return
    const missing = missingRequired(def.fields, values)
    if (missing.length) {
      setError(`${t('uc.required')}: ${missing.join(', ')}`)
      return
    }
    setError(null)
    setBusy(true)
    setOutput('')
    const ac = new AbortController()
    abortRef.current = ac
    try {
      // type=url のフィールドはサーバーで本文抽出して値を置換(UC-02)
      const resolved = { ...values }
      for (const f of def.fields) {
        if (f.type === 'url' && resolved[f.name]?.trim()) {
          setPhase('fetching')
          const res = await fetch('/api/tools/extract-url', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
            body: JSON.stringify({ url: resolved[f.name].trim() }),
            signal: ac.signal,
          })
          if (!res.ok) {
            const detail = await res.json().then((d) => d.detail).catch(() => res.status)
            throw new Error(`${t('uc.fetchFailed')}: ${detail}`)
          }
          const page = await res.json()
          resolved[f.name] = `【${page.title}】\n${page.text}`
        }
      }
      setPhase('streaming')
      const prompt = renderTemplate(def.template, resolved)
      const res = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
        body: JSON.stringify({
          source: `usecase:${id}`,
          model,
          messages: [{ role: 'user', content: prompt }],
        }),
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
          if (ev.delta) setOutput((cur) => cur + ev.delta)
          if (ev.error) setError(ev.error)
        },
        { signal: ac.signal },
      )
    } catch (e) {
      const aborted = e instanceof DOMException && e.name === 'AbortError'
      if (!aborted) setError(String(e instanceof Error ? e.message : e))
    } finally {
      setBusy(false)
      setPhase('idle')
      abortRef.current = null
    }
  }

  if (notFound) {
    return (
      <PageContainer icon="❓" title={t('uc.notFound')}>
        <Link to="/" className="text-action underline">
          {t('uc.backHome')}
        </Link>
      </PageContainer>
    )
  }
  if (!def) return null

  return (
    <PageContainer
      wide
      icon={def.icon || '🧩'}
      title={def.name}
      subtitle={def.description ?? ''}
      action={
        def.mine ? (
          <Link
            to={`/builder/${def.id ?? id}`}
            className="rounded-rw border border-line px-3 py-1.5 text-sm hover:border-action hover:text-action"
          >
            ✎ {t('uc.edit')}
          </Link>
        ) : undefined
      }
    >
      {/* 出力を広く取る(2:3)。min-w-0でグリッド子のはみ出しを防ぐ */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-5">
        <div className="min-w-0 rounded-rw border border-line bg-surface p-4 lg:col-span-2">
          <UcForm
            fields={def.fields}
            values={values}
            onChange={(name, v) => setValues((cur) => ({ ...cur, [name]: v }))}
            disabled={busy}
          />
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <select
              value={model}
              onChange={(e) => setModel(e.target.value)}
              disabled={busy}
              className="rounded-rw border border-line bg-surface px-2 py-2 text-xs outline-none focus:border-action"
              aria-label="model"
            >
              {(models.length ? models : [{ key: model, label: model }]).map((m) => (
                <option key={m.key} value={m.key}>
                  {m.label}
                </option>
              ))}
            </select>
            {busy ? (
              <button
                onClick={() => abortRef.current?.abort()}
                className="rounded-rw border border-line px-4 py-2 text-sm font-medium text-ink-muted hover:border-action hover:text-action"
              >
                ■ {t('chat.stop')}
              </button>
            ) : (
              <button
                onClick={() => void run()}
                className="rounded-rw bg-cta px-4 py-2 text-sm font-medium text-cta-ink hover:bg-cta-strong"
              >
                ▶ {t('uc.run')}
              </button>
            )}
            {phase === 'fetching' && (
              <span className="text-xs text-ink-muted">{t('uc.fetching')}</span>
            )}
            {error && <span className="text-xs text-primary-strong">⚠ {error}</span>}
          </div>
        </div>

        <div className="min-w-0 rounded-rw border border-line bg-surface p-4 lg:col-span-3">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-sm font-medium text-ink-muted">{t('uc.output')}</span>
            {output && !busy && (
              <button
                onClick={() => navigator.clipboard.writeText(output)}
                className="text-xs text-ink-muted hover:text-action"
              >
                ⧉ {t('chat.copy')}
              </button>
            )}
          </div>
          <div className="md min-h-40 text-sm leading-relaxed">
            {output ? <Md>{output}</Md> : (
              <p className="text-ink-muted/60">{busy ? '…' : t('uc.outputEmpty')}</p>
            )}
          </div>
        </div>
      </div>
    </PageContainer>
  )
}
