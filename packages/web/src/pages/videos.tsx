/** 映像の一覧・登録・分析(VID-06 / specs/20 §6 の `/videos`)。
 *
 *  **複数選択で登録できる**(specs/20 §2: 1 リクエスト 1 本なので画面が順に投げる)。
 *  分析状態は台帳の値をそのまま出す —— `failed` / `partial` を「分析済み」と同じ見た目に
 *  しない(specs/20 §3「握りつぶさない」を画面側でも守る)。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useUser } from '../auth'
import { PageContainer } from '../components/layout'
import { OciButton, Panel, StatusBadge } from '../components/oci'
import { usePrefs } from '../prefs'
import { analyzeAsset, deleteAsset, errorText, listAssets, uploadAsset } from './videos/api'
import { formatTimecode, formatUtc } from './videos/format'
import type { AnalysisState, VideoAsset } from './videos/types'

const PAGE_SIZE = 20

const STATE_BADGE: Record<AnalysisState, 'ok' | 'warn' | 'err' | 'neutral'> = {
  pending: 'neutral', running: 'warn', done: 'ok', failed: 'err', partial: 'warn',
}

function StateCell({ asset }: { asset: VideoAsset }) {
  const { t } = usePrefs()
  const state = (asset.analysis_state ?? 'pending') as AnalysisState
  return (
    <div className="min-w-0">
      <StatusBadge kind={STATE_BADGE[state] ?? 'neutral'}>
        {t(`videos.state.${state}` as Parameters<typeof t>[0])}
      </StatusBadge>
      {/* 失敗・一部のみは**理由まで出す**。「失敗した」だけでは利用者が直せない */}
      {asset.analysis_error && (
        <p className="mt-1 max-w-xs text-[10px] leading-snug text-primary-strong">
          {asset.analysis_error}
        </p>
      )}
    </div>
  )
}

export default function Videos() {
  const { t } = usePrefs()
  const user = useUser()
  const [assets, setAssets] = useState<VideoAsset[]>([])
  const [offset, setOffset] = useState(0)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [meta, setMeta] = useState({
    title: '', collection: '', category: '', rights: '', captured_at: '',
  })
  const [uploading, setUploading] = useState<{ done: number; total: number } | null>(null)
  const [uploadErrors, setUploadErrors] = useState<string[]>([])
  const [uploaded, setUploaded] = useState(0)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [keyword, setKeyword] = useState('')
  const [stateFilter, setStateFilter] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)

  const load = useCallback(
    (at = offset) =>
      listAssets(user, PAGE_SIZE, at)
        .then((d) => {
          setAssets(d.assets ?? [])
          setLoadError(null)
        })
        .catch((e) => {
          setAssets([])
          setLoadError(errorText(e))
        }),
    [user, offset],
  )

  useEffect(() => {
    void load(offset)
  }, [load, offset])

  // 分析中の映像があるあいだは一覧を更新する(別のタブや前回の実行が進むため)
  useEffect(() => {
    if (!assets.some((a) => a.analysis_state === 'running')) return
    const timer = setInterval(() => void load(offset), 5000)
    return () => clearInterval(timer)
  }, [assets, load, offset])

  /** 複数ファイルを**順に**投げる。1 本ずつ結果を確定させ、落ちたファイルは名前で残す
   *  —— まとめて「失敗しました」にすると、何本入って何本入らなかったのかが判らない。 */
  const upload = async (files: File[]) => {
    setUploading({ done: 0, total: files.length })
    setUploadErrors([])
    setUploaded(0)
    const failed: string[] = []
    let ok = 0
    for (const [i, file] of files.entries()) {
      try {
        await uploadAsset(user, file, { ...meta, title: files.length === 1 ? meta.title : '' })
        ok += 1
      } catch (e) {
        failed.push(`${file.name}: ${errorText(e)}`)
      }
      setUploading({ done: i + 1, total: files.length })
    }
    setUploaded(ok)
    setUploadErrors(failed)
    setUploading(null)
    if (fileRef.current) fileRef.current.value = ''
    setOffset(0)
    await load(0)
  }

  const analyze = async (asset: VideoAsset) => {
    setBusyId(asset.id)
    setActionError(null)
    // 押した瞬間に状態を進める(同期 API なので応答まで数分かかることがある)
    setAssets((cur) =>
      cur.map((a) => (a.id === asset.id ? { ...a, analysis_state: 'running' } : a)),
    )
    try {
      await analyzeAsset(user, asset.id)
    } catch (e) {
      setActionError(`${asset.title ?? asset.id}: ${errorText(e)}`)
    } finally {
      setBusyId(null)
      await load(offset)
    }
  }

  const remove = async (asset: VideoAsset) => {
    if (!window.confirm(t('videos.deleteConfirm'))) return
    setBusyId(asset.id)
    setActionError(null)
    try {
      await deleteAsset(user, asset.id)
    } catch (e) {
      setActionError(errorText(e))
    } finally {
      setBusyId(null)
      await load(offset)
    }
  }

  // **表示中のページに効く絞り込み**(全映像の横断は /videos/search)。
  const visible = useMemo(() => {
    const q = keyword.trim().toLowerCase()
    return assets.filter((a) => {
      if (stateFilter && (a.analysis_state ?? 'pending') !== stateFilter) return false
      if (!q) return true
      return [a.title, a.collection, a.category]
        .some((v) => (v ?? '').toLowerCase().includes(q))
    })
  }, [assets, keyword, stateFilter])

  return (
    <PageContainer
      wide
      icon="video"
      title={t('nav.videos')}
      subtitle={t('videos.lead')}
      action={
        <Link
          to="/videos/search"
          className="rounded-rw bg-cta px-3.5 py-1.5 text-sm font-medium text-cta-ink hover:bg-cta-strong"
        >
          🔍 {t('videos.toSearch')}
        </Link>
      }
    >
      <div className="space-y-6">
        <Panel title={t('videos.register')}>
          <div className="space-y-3 text-sm">
            <p className="text-[11px] text-ink-muted">{t('videos.formats')}</p>
            <p className="text-[11px] text-ink-muted">{t('videos.metaNote')}</p>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-5">
              {([
                ['title', 'videos.meta.title'],
                ['collection', 'videos.meta.collection'],
                ['category', 'videos.meta.category'],
                ['rights', 'videos.meta.rights'],
                ['captured_at', 'videos.meta.capturedAt'],
              ] as const).map(([key, labelKey]) => (
                <input
                  key={key}
                  value={meta[key]}
                  onChange={(e) => setMeta({ ...meta, [key]: e.target.value })}
                  placeholder={t(labelKey)}
                  aria-label={t(labelKey)}
                  className="w-full rounded-rw border border-line bg-bg px-2 py-1.5 text-sm outline-none focus:border-action"
                />
              ))}
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <input
                ref={fileRef}
                type="file"
                multiple
                accept="video/*,.mkv"
                aria-label={t('videos.pickFiles')}
                disabled={!!uploading}
                onChange={(e) => {
                  const files = [...(e.target.files ?? [])]
                  if (files.length) void upload(files)
                }}
                className="text-xs file:mr-3 file:rounded-rw file:border-0 file:bg-cta file:px-3 file:py-1.5 file:text-xs file:font-medium file:text-cta-ink"
              />
              {uploading && (
                <span className="text-xs text-ink-muted">
                  {t('videos.registering')} {uploading.done}/{uploading.total}
                </span>
              )}
              {!uploading && uploaded > 0 && (
                <span className="text-xs text-pill-ok-ink">
                  {uploaded} {t('videos.registered')}
                </span>
              )}
            </div>
            {uploadErrors.length > 0 && (
              <div className="rounded-rw bg-pill-err px-3 py-2 text-xs text-pill-err-ink">
                <p className="font-semibold">{t('videos.registerFailed')}</p>
                <ul className="mt-1 space-y-0.5">
                  {uploadErrors.map((e) => <li key={e}>{e}</li>)}
                </ul>
              </div>
            )}
            <p className="text-[11px] text-ink-muted">
              {t('videos.oneOffHint')} —{' '}
              <Link to="/video" className="text-action hover:underline">
                {t('nav.video')}
              </Link>
            </p>
          </div>
        </Panel>

        <Panel
          title={t('videos.list')}
          action={
            <span className="text-xs text-ink-muted">
              {t('videos.shown')} {visible.length}/{assets.length}
            </span>
          }
        >
          <div className="space-y-3">
            <div className="flex flex-wrap items-end gap-2">
              <label className="flex flex-col gap-1">
                <span className="text-[11px] text-ink-muted">{t('videos.filterKeyword')}</span>
                <input
                  value={keyword}
                  onChange={(e) => setKeyword(e.target.value)}
                  aria-label={t('videos.filter')}
                  className="w-56 rounded-rw border border-line bg-bg px-2 py-1.5 text-sm outline-none focus:border-action"
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[11px] text-ink-muted">{t('videos.f.analysisState')}</span>
                <select
                  value={stateFilter}
                  onChange={(e) => setStateFilter(e.target.value)}
                  aria-label={t('videos.f.analysisState')}
                  className="rounded-rw border border-line bg-bg px-2 py-1.5 text-sm outline-none focus:border-action"
                >
                  <option value="">{t('videos.opt.any')}</option>
                  {(['pending', 'running', 'done', 'failed', 'partial'] as const).map((s) => (
                    <option key={s} value={s}>
                      {t(`videos.state.${s}` as Parameters<typeof t>[0])}
                    </option>
                  ))}
                </select>
              </label>
              <p className="ml-auto max-w-md text-[11px] text-ink-muted">
                {t('videos.filterNote')}
              </p>
            </div>

            {loadError && (
              <p className="rounded-rw bg-pill-err px-3 py-2 text-sm text-pill-err-ink">
                ⚠ {loadError}
              </p>
            )}
            {actionError && (
              <p className="rounded-rw bg-pill-err px-3 py-2 text-sm text-pill-err-ink">
                ⚠ {actionError}
              </p>
            )}

            {visible.length === 0 ? (
              <p className="py-8 text-center text-sm text-ink-muted/70">{t('videos.empty')}</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full border-collapse text-left text-sm">
                  <thead>
                    <tr className="border-b border-line">
                      {['title', 'state', 'collection', 'category', 'captured', 'duration', 'actions']
                        .map((c) => (
                          <th
                            key={c}
                            scope="col"
                            className="px-3 py-2 text-xs font-bold text-ink"
                          >
                            {t(`videos.col.${c}` as Parameters<typeof t>[0])}
                          </th>
                        ))}
                    </tr>
                  </thead>
                  <tbody>
                    {visible.map((a) => (
                      <tr key={a.id} className="border-b border-line last:border-0 hover:bg-bg">
                        <td className="px-3 py-2.5">
                          <Link
                            to={`/videos/${a.id}`}
                            className="font-medium hover:text-action"
                            data-testid="asset-link"
                          >
                            {a.title || a.id}
                          </Link>
                          {a.summary && (
                            <p className="mt-0.5 line-clamp-2 max-w-md text-[11px] text-ink-muted">
                              {a.summary}
                            </p>
                          )}
                        </td>
                        <td className="px-3 py-2.5"><StateCell asset={a} /></td>
                        <td className="px-3 py-2.5 text-xs">{a.collection ?? ''}</td>
                        <td className="px-3 py-2.5 text-xs">{a.category ?? ''}</td>
                        <td className="px-3 py-2.5 text-xs tabular-nums">
                          {formatUtc(a.captured_at)}
                        </td>
                        <td className="px-3 py-2.5 text-xs tabular-nums">
                          {a.duration_ms ? formatTimecode(a.duration_ms) : ''}
                        </td>
                        <td className="px-3 py-2.5">
                          <div className="flex flex-wrap items-center gap-1.5">
                            <OciButton
                              variant="outline"
                              disabled={busyId === a.id || a.analysis_state === 'running'}
                              onClick={() => void analyze(a)}
                            >
                              {busyId === a.id || a.analysis_state === 'running'
                                ? t('videos.analyzing')
                                : a.analysis_state === 'pending'
                                  ? t('videos.analyze')
                                  : t('videos.reanalyze')}
                            </OciButton>
                            <Link
                              to={`/videos/${a.id}`}
                              className="rounded-rw px-2.5 py-1.5 text-xs text-action hover:bg-action-soft"
                            >
                              {t('videos.open')}
                            </Link>
                            <button
                              type="button"
                              disabled={busyId === a.id}
                              onClick={() => void remove(a)}
                              className="rounded-rw px-2 py-1.5 text-xs text-ink-muted hover:text-primary-strong disabled:opacity-40"
                            >
                              {t('videos.delete')}
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            <div className="flex items-center gap-2 text-xs">
              <p className="mr-auto text-ink-muted">{t('videos.analyzeNote')}</p>
              <OciButton
                variant="outline"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              >
                {t('videos.prev')}
              </OciButton>
              <OciButton
                variant="outline"
                disabled={assets.length < PAGE_SIZE}
                onClick={() => setOffset(offset + PAGE_SIZE)}
              >
                {t('videos.next')}
              </OciButton>
            </div>
          </div>
        </Panel>
      </div>
    </PageContainer>
  )
}
