/** マイデータ(CSV)パネル(dbchat.tsx分割: review-validation.md §7)。
 *  CSVアップロード / AIサンプルデータ生成 / データセット一覧 + プレビューを担う。
 *  ENH-01 / feedback 20260618-3 #1。 */
import { useEffect, useState } from 'react'
import { authHeaders, useUser } from '../../auth'
import { usePrefs } from '../../prefs'
import { PreviewTable } from './PreviewTable'
import type { Dataset, Result } from './types'

export function DatasetPanel({
  onError, model,
}: { onError: (msg: string) => void; model?: string }) {
  const { t } = usePrefs()
  const user = useUser()
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [uploading, setUploading] = useState(false)
  const [preview, setPreview] = useState<{ table: string; data: Result } | null>(null)
  const [previewing, setPreviewing] = useState(false)
  // AIサンプルデータ生成(feedback 20260618-3 #1)
  const [genOpen, setGenOpen] = useState(false)
  const [genDesc, setGenDesc] = useState('')
  const [genName, setGenName] = useState('')
  const [genRows, setGenRows] = useState(30)
  const [generatingDs, setGeneratingDs] = useState(false)
  // 既定サンプル投入(feedback 20260620 #12)と Select AI 認識待ち(#2)
  const [seeding, setSeeding] = useState(false)
  const [notReady, setNotReady] = useState(false)

  const loadDatasets = () =>
    fetch('/api/db/datasets', { headers: authHeaders(user) })
      .then((r) => r.json())
      .then((d) => setDatasets(d.datasets ?? []))
      .catch(() => setDatasets([]))

  useEffect(() => {
    void loadDatasets()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user])

  const uploadCsv = async (f: File | null) => {
    if (!f) return
    setUploading(true)
    onError('')
    try {
      const fd = new FormData()
      fd.append('file', f)
      const res = await fetch('/api/db/datasets', {
        method: 'POST',
        headers: authHeaders(user),
        body: fd,
      })
      const d = await res.json()
      if (!res.ok) throw new Error(typeof d.detail === 'string' ? d.detail : `HTTP ${res.status}`)
      setNotReady(d.ready === false)
      await loadDatasets()
    } catch (e) {
      onError(String(e instanceof Error ? e.message : e))
    } finally {
      setUploading(false)
    }
  }

  const seedSamples = async () => {
    if (seeding) return
    setSeeding(true)
    onError('')
    try {
      const res = await fetch('/api/db/datasets/seed', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
        body: JSON.stringify({ model: model || null }),
      })
      const d = await res.json()
      if (!res.ok) throw new Error(typeof d.detail === 'string' ? d.detail : `HTTP ${res.status}`)
      setNotReady(d.ready === false)
      await loadDatasets()
    } catch (e) {
      onError(String(e instanceof Error ? e.message : e))
    } finally {
      setSeeding(false)
    }
  }

  const generateDataset = async () => {
    const description = genDesc.trim()
    if (!description || generatingDs) return
    setGeneratingDs(true)
    onError('')
    try {
      const res = await fetch('/api/db/datasets/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
        body: JSON.stringify({
          description,
          display_name: genName.trim() || null,
          rows: genRows,
          model: model || null,
        }),
      })
      const d = await res.json()
      if (!res.ok) throw new Error(typeof d.detail === 'string' ? d.detail : `HTTP ${res.status}`)
      setNotReady(d.ready === false)
      await loadDatasets()
      setGenDesc('')
      setGenName('')
      setGenOpen(false)
    } catch (e) {
      onError(String(e instanceof Error ? e.message : e))
    } finally {
      setGeneratingDs(false)
    }
  }

  const deleteDataset = async (id: string) => {
    await fetch(`/api/db/datasets/${id}`, { method: 'DELETE', headers: authHeaders(user) })
    void loadDatasets()
  }

  const loadDatasetPreview = async (id: string) => {
    setPreviewing(true)
    setPreview(null)
    try {
      const res = await fetch(`/api/db/datasets/${id}/preview`, { headers: authHeaders(user) })
      if (res.ok) setPreview({ table: `ds:${id}`, data: await res.json() })
    } finally {
      setPreviewing(false)
    }
  }

  return (
    <div className="rounded-rw border border-line bg-surface px-4 py-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <input
          type="file"
          accept=".csv,text/csv"
          id="ds-upload"
          className="hidden"
          onChange={(e) => {
            void uploadCsv(e.target.files?.[0] ?? null)
            e.target.value = ''
          }}
        />
        <label
          htmlFor="ds-upload"
          className="cursor-pointer rounded-rw bg-cta px-3 py-1.5 text-xs font-medium text-cta-ink hover:bg-cta-strong"
        >
          {uploading ? t('db.dataset.uploading') : `＋ ${t('db.dataset.upload')}`}
        </label>
        <button
          type="button"
          onClick={() => setGenOpen((v) => !v)}
          className={`rounded-rw border px-3 py-1.5 text-xs font-medium ${
            genOpen
              ? 'border-action bg-action-soft text-ink'
              : 'border-line text-ink-muted hover:border-action hover:text-action'
          }`}
        >
          ✨ {t('db.dataset.generate')}
        </button>
        {/* 既定サンプルをワンクリック投入(feedback 20260620 #12) */}
        <button
          type="button"
          onClick={() => void seedSamples()}
          disabled={seeding}
          className="rounded-rw border border-line px-3 py-1.5 text-xs font-medium text-ink-muted hover:border-action hover:text-action disabled:opacity-40"
        >
          {seeding ? t('db.dataset.seeding') : `📦 ${t('db.dataset.seed')}`}
        </button>
        <span className="text-[11px] text-ink-muted">{t('db.dataset.hint')}</span>
      </div>

      {/* Select AI がデータを認識するまでの案内(feedback 20260620 #2) */}
      {notReady && (
        <div className="mb-2 rounded-rw border border-band-chip/40 bg-band-chip/10 px-3 py-1.5 text-[11px] text-ink-muted">
          ⏳ {t('db.dataset.notReady')}
        </div>
      )}

      {/* AIサンプルデータ生成フォーム(feedback 20260618-3 #1) */}
      {genOpen && (
        <div className="mb-3 space-y-2 rounded-rw border border-action bg-bg px-3 py-2.5">
          <p className="text-[11px] text-ink-muted">{t('db.dataset.genHint')}</p>
          <textarea
            rows={2}
            value={genDesc}
            onChange={(e) => setGenDesc(e.target.value)}
            placeholder={t('db.dataset.genPlaceholder')}
            className="w-full resize-y rounded-rw border border-line bg-surface px-2.5 py-1.5 text-xs outline-none focus:border-action"
          />
          <div className="flex flex-wrap items-center gap-2">
            <input
              value={genName}
              onChange={(e) => setGenName(e.target.value)}
              placeholder={t('db.dataset.genName')}
              className="min-w-40 flex-1 rounded-rw border border-line bg-surface px-2.5 py-1.5 text-xs outline-none focus:border-action"
            />
            <label className="flex items-center gap-1.5 text-[11px] text-ink-muted">
              {t('db.dataset.genRows')}
              <input
                type="number"
                min={1}
                max={200}
                value={genRows}
                onChange={(e) =>
                  setGenRows(Math.max(1, Math.min(200, Number(e.target.value) || 1)))
                }
                className="w-16 rounded-rw border border-line bg-surface px-2 py-1.5 text-xs outline-none focus:border-action"
              />
            </label>
            <button
              type="button"
              onClick={() => void generateDataset()}
              disabled={!genDesc.trim() || generatingDs}
              className="rounded-rw bg-cta px-3 py-1.5 text-xs font-medium text-cta-ink hover:bg-cta-strong disabled:opacity-40"
            >
              {generatingDs ? t('db.dataset.generating') : `✨ ${t('db.dataset.genSubmit')}`}
            </button>
          </div>
        </div>
      )}
      {datasets.length === 0 ? (
        <p className="text-xs text-ink-muted">
          {t('db.dataset.empty')}{' '}
          <button
            type="button"
            onClick={() => void seedSamples()}
            disabled={seeding}
            className="text-action hover:underline disabled:opacity-40"
          >
            {seeding ? t('db.dataset.seeding') : t('db.dataset.seed')}
          </button>
        </p>
      ) : (
        <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
          {datasets.map((d) => (
            <div key={d.id} className="rounded-rw border border-line bg-bg px-2.5 py-1.5">
              <div className="flex items-baseline justify-between gap-2">
                <span className="truncate text-xs font-semibold">{d.display_name}</span>
                <span className="shrink-0 text-[10px] text-ink-muted">
                  {d.row_count.toLocaleString()}
                  {t('db.rows')}
                </span>
              </div>
              <div className="mt-0.5 truncate font-mono text-[10px] text-ink-muted">
                {d.columns.join(', ')}
              </div>
              <div className="mt-1 flex gap-2 text-[11px]">
                <button
                  onClick={() => void loadDatasetPreview(d.id)}
                  disabled={previewing}
                  className="text-action hover:underline disabled:opacity-40"
                >
                  {t('db.preview')}
                </button>
                <button
                  onClick={() => void deleteDataset(d.id)}
                  className="text-primary hover:underline"
                >
                  {t('chat.preset.delete')}
                </button>
              </div>
              {preview?.table === `ds:${d.id}` && <PreviewTable data={preview.data} />}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
