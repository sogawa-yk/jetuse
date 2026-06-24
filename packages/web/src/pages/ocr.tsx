/** OCR / 文書認識(ENH-07): 画像/PDF→OCR(OCI Document Understanding / VLM 選択式)→テキスト/表/KV表示 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { authHeaders, reauthenticate, useUser } from '../auth'
import { PageContainer } from '../components/layout'
import { OciButton, Panel, StatusBadge } from '../components/oci'
import { usePrefs } from '../prefs'

type OcrResult = {
  text: string
  page_count: number
  mean_confidence: number | null
  chunk_count: number
  engine?: string
  model?: string
  tables: { rows: string[][]; row_count: number; column_count: number }[]
  key_values: { label: string | null; value: string | null }[]
}

export default function Ocr() {
  const { t } = usePrefs()
  const user = useUser()
  const nav = useNavigate()
  const [engine, setEngine] = useState('document_understanding')
  const [vlmModel, setVlmModel] = useState('google.gemini-2.5-pro')
  const [language, setLanguage] = useState('JPN')
  const [tables, setTables] = useState(true)
  const [keyValues, setKeyValues] = useState(false)
  const [langOpts, setLangOpts] = useState<{ code: string; label: string }[]>([])
  const [engineOpts, setEngineOpts] = useState<{ name: string; label: string }[]>([])
  const [vlmModelOpts, setVlmModelOpts] = useState<{ key: string; label: string }[]>([])
  const [maxPages, setMaxPages] = useState<number | null>(null)
  const [file, setFile] = useState<File | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<OcrResult | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const isVlm = engine === 'vlm'
  // DUの表抽出は英語のみ(SPIKE-E4)。VLMは日本語の表も可
  const tablesSupported = isVlm || language === 'ENG'

  useEffect(() => {
    fetch('/api/ocr/options', { headers: authHeaders(user) })
      .then((r) => r.json())
      .then((d) => {
        setLangOpts(d.languages ?? [])
        setMaxPages(d.max_pages ?? null)
        setEngineOpts(d.engines ?? [])
        setVlmModelOpts(d.vlm_models ?? [])
      })
      .catch(() => undefined)
  }, [user])

  // 画像のサムネイル(feedback 20260620 #11)。objectURLは差し替え/アンマウント時に解放。PDFはなし
  const previewUrl = useMemo(
    () => (file && file.type.startsWith('image/') ? URL.createObjectURL(file) : null),
    [file],
  )
  useEffect(() => {
    if (!previewUrl) return
    return () => URL.revokeObjectURL(previewUrl)
  }, [previewUrl])

  // クリップボードの画像を Ctrl+V で貼り付け(feedback 20260620 #10)
  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      if (busy) return
      const items = e.clipboardData?.items
      if (!items) return
      for (const it of items) {
        if (it.type.startsWith('image/')) {
          const f = it.getAsFile()
          if (f) {
            setFile(f)
            setError(null)
            break
          }
        }
      }
    }
    window.addEventListener('paste', onPaste)
    return () => window.removeEventListener('paste', onPaste)
  }, [busy])

  const run = async () => {
    if (!file || busy) return
    setBusy(true)
    setError(null)
    setResult(null)
    try {
      const fd = new FormData()
      fd.append('file', file)
      const qs = new URLSearchParams({
        engine,
        model: vlmModel,
        language,
        tables: String(tables && tablesSupported),
        key_values: String(keyValues),
      })
      const res = await fetch(`/api/ocr?${qs}`, {
        method: 'POST',
        headers: authHeaders(user),
        body: fd,
      })
      if (res.status === 401) return reauthenticate()
      const data = await res.json()
      if (!res.ok)
        throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`)
      setResult(data)
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e))
    } finally {
      setBusy(false)
    }
  }

  const toChat = () => {
    if (!result?.text) return
    sessionStorage.setItem('ocr.toChat', result.text)
    nav('/chat')
  }

  return (
    <PageContainer icon="ocr" title={t('nav.ocr')} subtitle={t('ocr.lead')} helpKey="ocr">
      <Panel
        title={t('ocr.title')}
        action={
          <div className="flex items-center gap-2 text-xs">
            <select
              value={engine}
              onChange={(e) => setEngine(e.target.value)}
              disabled={busy}
              className="rounded-rw border border-line bg-surface px-2 py-1"
              aria-label={t('ocr.engine')}
              title={t('ocr.engine')}
            >
              {engineOpts.map((en) => (
                <option key={en.name} value={en.name}>{en.label}</option>
              ))}
            </select>
            {isVlm ? (
              <select
                value={vlmModel}
                onChange={(e) => setVlmModel(e.target.value)}
                disabled={busy}
                className="rounded-rw border border-line bg-surface px-2 py-1"
                aria-label={t('ocr.model')}
              >
                {vlmModelOpts.map((m) => (
                  <option key={m.key} value={m.key}>{m.label}</option>
                ))}
              </select>
            ) : (
              <select
                value={language}
                onChange={(e) => setLanguage(e.target.value)}
                disabled={busy}
                className="rounded-rw border border-line bg-surface px-2 py-1"
                aria-label={t('ocr.language')}
              >
                {langOpts.map((l) => (
                  <option key={l.code} value={l.code}>{l.label}</option>
                ))}
              </select>
            )}
            <OciButton onClick={() => void run()} disabled={!file || busy}>
              {busy ? t('ocr.running') : t('ocr.run')}
            </OciButton>
          </div>
        }
      >
        {error && (
          <div className="mb-3 rounded-rw bg-pill-err px-3 py-2 text-sm text-pill-err-ink">
            {error}
          </div>
        )}
        <div className="mb-3 space-y-3 border-b border-line pb-3 text-sm">
          <div className="flex flex-wrap items-center gap-3">
            <input
              ref={fileRef}
              type="file"
              accept=".png,.jpg,.jpeg,.tiff,.tif,.pdf,image/*,application/pdf"
              disabled={busy}
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              className="block text-xs file:mr-2 file:rounded-rw file:border file:border-line file:bg-surface file:px-3 file:py-1 file:text-xs"
            />
            {/* 抽出元のサムネイル(画像) / PDFはファイル名表示(feedback 20260620 #11) */}
            {previewUrl ? (
              <img
                src={previewUrl}
                alt={file?.name ?? 'preview'}
                className="h-16 w-16 rounded-rw border border-line object-cover"
              />
            ) : (
              file && (
                <span className="rounded-rw border border-line bg-bg px-2 py-1 text-[11px] text-ink-muted">
                  📄 {file.name}
                </span>
              )
            )}
          </div>
          <p className="text-[11px] text-ink-muted">{t('ocr.pasteHint')}</p>
          {/* 抽出フィーチャは1リクエストで同時指定可(複数選択式)。テキスト抽出=常時オンの基準 */}
          <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
            <span className="text-ink-muted">{t('ocr.extractWhat')}:</span>
            <label className="flex items-center gap-2 text-ink-muted">
              <input type="checkbox" checked disabled />
              {t('ocr.text')}
              <span className="text-[11px]">（{t('ocr.textAlways')}）</span>
            </label>
            {/* 表抽出はOCI Document Understandingの仕様上、英語(ENG)選択時のみ対応 */}
            {tablesSupported && (
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={tables}
                  onChange={(e) => setTables(e.target.checked)}
                />
                {t('ocr.tables')}
              </label>
            )}
            {/* キー/値抽出はDU(Document Understanding)エンジン専用 */}
            {!isVlm && (
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={keyValues}
                  onChange={(e) => setKeyValues(e.target.checked)}
                />
                {t('ocr.kv')}
              </label>
            )}
          </div>
        </div>
        {isVlm ? (
          <p className="mb-3 text-[11px] text-ink-muted">{t('ocr.vlmNote')}</p>
        ) : (
          <>
            {!tablesSupported && (
              <p className="mb-3 text-[11px] text-ink-muted">{t('ocr.tablesEngOnly')}</p>
            )}
            {maxPages != null && (
              <p className="mb-3 text-[11px] text-ink-muted">
                {t('ocr.pageLimit').replaceAll('{n}', String(maxPages))}
              </p>
            )}
          </>
        )}

        {!result ? (
          <p className="text-xs text-ink-muted">{busy ? t('ocr.running') : t('ocr.hint')}</p>
        ) : (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-3 text-xs text-ink-muted">
              <StatusBadge kind="ok">
                {t('ocr.pages')}: {result.page_count}
              </StatusBadge>
              {result.engine === 'vlm' && <span>VLM: {result.model}</span>}
              {result.chunk_count > 1 && (
                <span>{t('ocr.chunks').replace('{n}', String(result.chunk_count))}</span>
              )}
              {result.mean_confidence != null && (
                <span>
                  {t('ocr.confidence')}: {(result.mean_confidence * 100).toFixed(1)}%
                </span>
              )}
              {result.text && (
                <>
                  <OciButton
                    variant="ghost"
                    onClick={() => navigator.clipboard.writeText(result.text)}
                  >
                    {t('chat.copy')}
                  </OciButton>
                  <OciButton variant="outline" onClick={toChat}>
                    {t('ocr.toChat')}
                  </OciButton>
                </>
              )}
            </div>

            <div>
              <span className="mb-1 block text-xs font-medium text-ink-muted">
                {t('ocr.result')}
              </span>
              {result.text ? (
                <pre className="max-h-[28rem] overflow-auto whitespace-pre-wrap rounded-rw border border-line bg-surface p-3 text-sm">
                  {result.text}
                </pre>
              ) : (
                <p className="text-xs text-ink-muted">{t('ocr.empty')}</p>
              )}
            </div>

            {result.tables.length > 0 && (
              <div>
                <span className="mb-1 block text-xs font-medium text-ink-muted">
                  {t('ocr.tablesResult')}
                </span>
                {result.tables.map((tb, ti) => (
                  <div key={ti} className="mb-3 overflow-auto rounded-rw border border-line">
                    <table className="w-full border-collapse text-sm">
                      <tbody>
                        {tb.rows.map((row, ri) => (
                          <tr key={ri} className="border-b border-line last:border-0">
                            {row.map((cell, ci) => (
                              <td key={ci} className="border-r border-line px-2 py-1 last:border-0">
                                {cell}
                              </td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ))}
              </div>
            )}

            {result.key_values.length > 0 && (
              <div>
                <span className="mb-1 block text-xs font-medium text-ink-muted">
                  {t('ocr.kvResult')}
                </span>
                <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
                  {result.key_values.map((kv, i) => (
                    <div key={i} className="contents">
                      <dt className="text-ink-muted">{kv.label ?? '—'}</dt>
                      <dd>{kv.value ?? '—'}</dd>
                    </div>
                  ))}
                </dl>
              </div>
            )}
          </div>
        )}
      </Panel>
    </PageContainer>
  )
}
