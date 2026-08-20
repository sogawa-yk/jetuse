/** 映像の詳細(VID-06 / specs/20 §6 の `/videos/{id}`)。
 *
 *  プレーヤ + **タイムライン**(場面帯を種別で色分け・選ぶとその時刻へ移動 = 要求7)と、
 *  場面ごとの修正・確認・再分析(要求8)。検索結果から `?t=<秒>` で来たときは
 *  **その時刻から再生する**(要求6。この機能の主要な価値)。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useUser } from '../../auth'
import { PageContainer } from '../../components/layout'
import { OciButton, Panel, StatusBadge } from '../../components/oci'
import { usePrefs } from '../../prefs'
import {
  analyzeAsset, confirmScene, deleteScene, errorText, getAsset, getPlayback,
  listSceneEdits, patchScene,
} from './api'
import {
  asObject, asStringList, fieldLabel, formatRange, formatTimecode, formatUtc,
  parseSeekSeconds, sceneKindColor,
} from './format'
import { Timeline } from './Timeline'
import type {
  AnalysisState, SceneEdit, SceneSource, VideoAssetDetail, VideoScene,
} from './types'

const STATE_BADGE: Record<AnalysisState, 'ok' | 'warn' | 'err' | 'neutral'> = {
  pending: 'neutral', running: 'warn', done: 'ok', failed: 'err', partial: 'warn',
}

/** 場面のうち人が直せる項目(API の `EDITABLE_FIELDS` と同じ)。 */
type EditForm = {
  description: string
  tags: string
  screen_text: string
  place: string
  scene_kind: string
}

const toForm = (scene: VideoScene): EditForm => ({
  description: scene.description ?? '',
  tags: asStringList(scene.tags).join(', '),
  screen_text: scene.screen_text ?? '',
  place: scene.place ?? '',
  scene_kind: scene.scene_kind ?? '',
})

/** ルートの入口。**URL の指す先が変わったら作り直す**。
 *
 *  頭出し(`?t=`)は `loadedMetadata` の 1 回だけ当てる作りなので、同じ詳細画面を使い回すと
 *  「検索結果 → 詳細 → 別の結果（同じ映像の別の場面）」で 2 回目の頭出しが効かない
 *  (Codex review-2 の指摘)。`key` を URL から作れば、React が状態ごと作り直す。
 */
export default function VideoDetailRoute() {
  const { id = '' } = useParams()
  const [params] = useSearchParams()
  return <VideoDetail key={`${id}|${params.get('t') ?? ''}|${params.get('scene') ?? ''}`} />
}

function VideoDetail() {
  const { t } = usePrefs()
  const user = useUser()
  const { id = '' } = useParams()
  const [params] = useSearchParams()
  const [asset, setAsset] = useState<VideoAssetDetail | null>(null)
  const [playbackUrl, setPlaybackUrl] = useState<string | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [playbackError, setPlaybackError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(params.get('scene'))
  const [currentMs, setCurrentMs] = useState(0)
  const [analyzing, setAnalyzing] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const videoRef = useRef<HTMLVideoElement>(null)
  // **頭出しを当てた src** を覚える(単なる「1 回やった」フラグでは足りない)。
  // 再生 URL は期限付き PAR で、取り直すと `src` が変わり **メディアが読み直されて
  // 再生位置が 0 に戻る**。フラグだけだと「一度当てた」ことになっていて当て直せず、
  // 検索結果から来たのに先頭から再生される(実ブラウザで踏んだ: React StrictMode の
  // 二重マウントで PAR が 2 回発行され、2 本目の URL で位置が消えた)。
  const seekAppliedFor = useRef<string | null>(null)
  // 利用者が自分で場面を選んだら、以降 `?t=` は当てない(操作を上書きしない)
  const userSeeked = useRef(false)

  const seekSeconds = parseSeekSeconds(params.get('t'))
  const badSeek = params.get('t') != null && seekSeconds === null

  // 取得は promise の連鎖で書く(effect の中で同期に setState しない)。
  const load = useCallback(
    () =>
      getAsset(user, id)
        .then((detail) => { setAsset(detail); setLoadError(null) })
        .catch((e) => { setAsset(null); setLoadError(errorText(e)) }),
    [user, id],
  )

  useEffect(() => {
    void load()
    getPlayback(user, id)
      .then((d) => { setPlaybackUrl(d.url); setPlaybackError(null) })
      .catch((e) => { setPlaybackUrl(null); setPlaybackError(errorText(e)) })
  }, [load, user, id])

  /** `?t=` の頭出し。**同じ映像には 1 回だけ**当てる(利用者の操作は上書きしない)。 */
  const onLoadedMetadata = () => {
    const video = videoRef.current
    if (!video || seekSeconds === null || userSeeked.current) return
    const src = video.currentSrc || video.src
    if (seekAppliedFor.current === src) return
    seekAppliedFor.current = src
    video.currentTime = seekSeconds
    setCurrentMs(seekSeconds * 1000)
    // 自動再生はブラウザの方針で拒まれることがある。拒まれても**位置は合っている**ので
    // 利用者は再生を押すだけでよい(拒否を握りつぶさず、位置合わせは必ず先に済ませる)。
    // `play()` が Promise を返さない実装(古いブラウザ / jsdom)でも落ちないようにする
    void Promise.resolve(video.play()).catch(() => undefined)
  }

  /** 帯・一覧から場面を選ぶ = **その時刻へ移動する**(要求7)。 */
  const seekTo = (scene: VideoScene) => {
    setSelectedId(scene.id)
    const video = videoRef.current
    if (!video) return
    userSeeked.current = true  // 以降は ?t= を当てない(利用者の操作が優先)
    video.currentTime = scene.start_ms / 1000
    setCurrentMs(scene.start_ms)
    void Promise.resolve(video.play()).catch(() => undefined)
  }

  const scenes = useMemo(() => asset?.scenes ?? [], [asset])
  // 選択中の場面。指定が無ければ**再生位置に重なる場面**(見ている場面を選んでおく)
  const selected = useMemo(() => {
    if (selectedId) {
      const hit = scenes.find((s) => s.id === selectedId)
      if (hit) return hit
    }
    return scenes.find((s) => currentMs >= s.start_ms && currentMs < s.end_ms) ?? scenes[0] ?? null
  }, [scenes, selectedId, currentMs])

  const analyze = async () => {
    setAnalyzing(true)
    setActionError(null)
    try {
      await analyzeAsset(user, id)
    } catch (e) {
      setActionError(errorText(e))
    } finally {
      setAnalyzing(false)
      await load()
    }
  }

  const state = (asset?.analysis_state ?? 'pending') as AnalysisState

  return (
    <PageContainer
      wide
      icon="video"
      title={asset?.title || t('nav.videos')}
      subtitle={asset ? [asset.collection, asset.category].filter(Boolean).join(' ・ ') : ''}
      action={
        <div className="flex items-center gap-2">
          <Link
            to="/videos/search"
            className="rounded-rw border border-line px-3 py-1.5 text-sm hover:border-action hover:text-action"
          >
            {t('videos.toSearch')}
          </Link>
          <Link
            to="/videos"
            className="rounded-rw border border-line px-3 py-1.5 text-sm hover:border-action hover:text-action"
          >
            {t('videos.toList')}
          </Link>
        </div>
      }
    >
      {loadError ? (
        <p className="rounded-rw bg-pill-err px-3 py-2 text-sm text-pill-err-ink">
          ⚠ {loadError}
        </p>
      ) : (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-5">
          {/* --- プレーヤ + タイムライン --- */}
          <div className="min-w-0 space-y-4 lg:col-span-3">
            <Panel>
              <div className="space-y-3">
                {seekSeconds !== null && (
                  <p className="rounded-rw bg-action-soft px-3 py-1.5 text-[11px] text-ink">
                    ▶ {t('videos.detail.fromSearch')}（{formatTimecode(seekSeconds * 1000)}）
                  </p>
                )}
                {badSeek && (
                  <p className="rounded-rw bg-pill-warn px-3 py-1.5 text-[11px] text-pill-warn-ink">
                    {t('videos.detail.badSeek')}
                  </p>
                )}
                {playbackError && (
                  <p className="rounded-rw bg-pill-err px-3 py-2 text-sm text-pill-err-ink">
                    ⚠ {t('videos.detail.playbackFailed')}: {playbackError}
                  </p>
                )}
                {playbackUrl && (
                  <video
                    ref={videoRef}
                    src={playbackUrl}
                    controls
                    playsInline
                    preload="metadata"
                    data-testid="video-player"
                    onLoadedMetadata={onLoadedMetadata}
                    onTimeUpdate={(e) =>
                      setCurrentMs(Math.round(e.currentTarget.currentTime * 1000))
                    }
                    className="aspect-video w-full rounded-rw bg-black"
                  />
                )}

                <div>
                  <div className="mb-1 flex items-center gap-2">
                    <h2 className="text-sm font-bold">{t('videos.detail.timeline')}</h2>
                    <span className="text-[11px] tabular-nums text-ink-muted">
                      {formatTimecode(currentMs)}
                      {asset?.duration_ms ? ` / ${formatTimecode(asset.duration_ms)}` : ''}
                    </span>
                  </div>
                  <Timeline
                    scenes={scenes}
                    durationMs={asset?.duration_ms ?? null}
                    currentMs={currentMs}
                    selectedId={selected?.id ?? null}
                    onPick={seekTo}
                  />
                </div>
              </div>
            </Panel>

            <Panel title={t('videos.detail.analysisError')}>
              <div className="space-y-2 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <StatusBadge kind={STATE_BADGE[state] ?? 'neutral'}>
                    {t(`videos.state.${state}` as Parameters<typeof t>[0])}
                  </StatusBadge>
                  <span className="text-xs text-ink-muted">
                    {t('videos.detail.scenes')} {scenes.length}
                  </span>
                  <span className="text-xs text-ink-muted">
                    {t('videos.field.created_at')} {formatUtc(asset?.created_at)}
                  </span>
                  {asset?.captured_at && (
                    <span className="text-xs text-ink-muted">
                      {t('videos.field.captured_at')} {formatUtc(asset.captured_at)}
                    </span>
                  )}
                  <OciButton
                    variant="outline"
                    className="ml-auto"
                    disabled={analyzing || state === 'running'}
                    onClick={() => void analyze()}
                  >
                    {analyzing || state === 'running'
                      ? t('videos.analyzing')
                      : t('videos.reanalyze')}
                  </OciButton>
                </div>
                {asset?.analysis_error && (
                  <p className="rounded-rw bg-pill-warn px-3 py-2 text-xs text-pill-warn-ink">
                    {asset.analysis_error}
                  </p>
                )}
                {actionError && (
                  <p className="rounded-rw bg-pill-err px-3 py-2 text-xs text-pill-err-ink">
                    ⚠ {actionError}
                  </p>
                )}
                {asset?.summary && (
                  <div>
                    <h3 className="text-xs font-bold text-ink-muted">{t('videos.summary')}</h3>
                    <p className="mt-0.5 whitespace-pre-wrap text-xs leading-relaxed">
                      {asset.summary}
                    </p>
                  </div>
                )}
                {asset?.analysis_error == null && analyzing && (
                  <p className="text-[11px] text-ink-muted">{t('videos.analyzeNote')}</p>
                )}
              </div>
            </Panel>
          </div>

          {/* --- 場面の一覧と編集 --- */}
          <div className="min-w-0 space-y-4 lg:col-span-2">
            <Panel title={`${t('videos.detail.scenes')} (${scenes.length})`}>
              {scenes.length === 0 ? (
                <p className="py-6 text-center text-sm text-ink-muted/70">
                  {t('videos.detail.noScenes')}
                </p>
              ) : (
                <ul className="max-h-64 space-y-1 overflow-y-auto pr-1">
                  {scenes.map((s) => (
                    <li key={s.id}>
                      <button
                        type="button"
                        onClick={() => seekTo(s)}
                        data-testid="scene-row"
                        className={`flex w-full items-start gap-2 rounded-rw border px-2 py-1.5 text-left text-xs transition-colors ${
                          selected?.id === s.id
                            ? 'border-action bg-action-soft'
                            : 'border-line hover:bg-bg'
                        }`}
                      >
                        <span
                          aria-hidden
                          className="mt-0.5 h-3 w-3 shrink-0 rounded-sm"
                          style={{ backgroundColor: sceneKindColor(s.scene_kind) }}
                        />
                        <span className="shrink-0 tabular-nums text-ink-muted">
                          {formatTimecode(s.start_ms)}
                        </span>
                        <span className="min-w-0 flex-1 truncate">{s.description}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </Panel>

            {selected ? (
              <SceneEditor
                key={selected.id}
                scene={selected}
                assetState={state}
                onSeek={() => seekTo(selected)}
                onUpdated={(next) =>
                  setAsset((cur) =>
                    cur
                      ? { ...cur, scenes: cur.scenes.map((s) => (s.id === next.id ? next : s)) }
                      : cur,
                  )
                }
                onDeleted={() =>
                  setAsset((cur) =>
                    cur
                      ? { ...cur, scenes: cur.scenes.filter((s) => s.id !== selected.id) }
                      : cur,
                  )
                }
              />
            ) : (
              <Panel>
                <p className="text-xs text-ink-muted">{t('videos.detail.selectScene')}</p>
              </Panel>
            )}
          </div>
        </div>
      )}
    </PageContainer>
  )
}

/** 1 場面の確認・修正(要求8)。**出所(source)を隠さない** —— 誰の言葉かが判るようにする。 */
function SceneEditor({
  scene, assetState, onSeek, onUpdated, onDeleted,
}: {
  scene: VideoScene
  assetState: AnalysisState
  onSeek: () => void
  onUpdated: (scene: VideoScene) => void
  onDeleted: () => void
}) {
  const { t } = usePrefs()
  const user = useUser()
  const [editing, setEditing] = useState(false)
  const [form, setForm] = useState<EditForm>(() => toForm(scene))
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [warning, setWarning] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [edits, setEdits] = useState<SceneEdit[] | null>(null)

  const source = (scene.source ?? 'ai') as SceneSource
  const people = asObject(scene.people)

  const save = async () => {
    setBusy(true)
    setError(null)
    setMessage(null)
    setWarning(null)
    const base = toForm(scene)
    // **変えた項目だけ送る**(API は `exclude_unset` で受ける)。触っていない項目まで
    // 送ると、履歴に「直していない項目」が並んで何を直したのか判らなくなる
    const changes: Record<string, unknown> = {}
    if (form.description !== base.description) changes.description = form.description
    if (form.tags !== base.tags) {
      changes.tags = form.tags.split(/[,、]/u).map((s) => s.trim()).filter(Boolean)
    }
    if (form.screen_text !== base.screen_text) changes.screen_text = form.screen_text
    if (form.place !== base.place) changes.place = form.place
    if (form.scene_kind !== base.scene_kind) changes.scene_kind = form.scene_kind
    if (!Object.keys(changes).length) {
      setEditing(false)
      setBusy(false)
      return
    }
    try {
      const updated = await patchScene(user, scene.id, changes)
      onUpdated(updated)
      setEditing(false)
      // 埋め込みを作り直せなかった場面は自然言語検索に出ない。**成功と失敗を同時に出さない**
      // (「作り直しました」と「作り直せませんでした」が並ぶと、どちらが起きたのか判らない)
      if (updated.embedding_state === 'ok') {
        setMessage(t('videos.detail.saved'))
      } else {
        setMessage(t('videos.detail.savedNoEmbedding'))
        setWarning(`${t('videos.detail.embedFailed')}: ${updated.embedding_error ?? ''}`)
      }
      if (edits) setEdits((await listSceneEdits(user, scene.id)).edits)
    } catch (e) {
      setError(errorText(e))
    } finally {
      setBusy(false)
    }
  }

  const confirm = async () => {
    setBusy(true)
    setError(null)
    try {
      onUpdated(await confirmScene(user, scene.id))
      setMessage(t('videos.opt.confirmedYes'))
    } catch (e) {
      setError(errorText(e))
    } finally {
      setBusy(false)
    }
  }

  const remove = async () => {
    if (!window.confirm(t('videos.detail.deleteSceneConfirm'))) return
    setBusy(true)
    setError(null)
    try {
      await deleteScene(user, scene.id)
      onDeleted()
    } catch (e) {
      setError(errorText(e))
    } finally {
      setBusy(false)
    }
  }

  const toggleHistory = async () => {
    if (edits) return setEdits(null)
    try {
      setEdits((await listSceneEdits(user, scene.id)).edits)
    } catch (e) {
      setError(errorText(e))
    }
  }

  const field = (label: string, value: string) =>
    value && value !== 'unknown' ? (
      <p className="text-xs">
        <span className="text-ink-muted">{label}: </span>
        {value}
      </p>
    ) : null

  const input =
    'w-full rounded-rw border border-line bg-bg px-2 py-1.5 text-sm outline-none focus:border-action'

  return (
    <Panel
      title={formatRange(scene.start_ms, scene.end_ms)}
      action={
        <button
          type="button"
          onClick={onSeek}
          className="rounded-rw border border-line px-2.5 py-1 text-xs text-ink-muted hover:border-action hover:text-action"
        >
          ▶ {t('videos.detail.seek')}
        </button>
      }
    >
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge kind={source === 'ai' ? 'neutral' : 'ok'}>
            {t(`videos.detail.source.${source}` as Parameters<typeof t>[0])}
          </StatusBadge>
          {scene.confirmed_at && (
            <span className="text-[11px] text-ink-muted">
              {t('videos.field.confirmed_at')} {formatUtc(scene.confirmed_at)}
            </span>
          )}
        </div>

        {message && (
          <p className="rounded-rw bg-pill-ok px-3 py-1.5 text-xs text-pill-ok-ink">{message}</p>
        )}
        {warning && (
          <p className="rounded-rw bg-pill-warn px-3 py-1.5 text-xs text-pill-warn-ink">
            {warning}
          </p>
        )}
        {error && (
          <p className="rounded-rw bg-pill-err px-3 py-1.5 text-xs text-pill-err-ink">⚠ {error}</p>
        )}

        {editing ? (
          <div className="space-y-2">
            <label className="block">
              <span className="text-[11px] text-ink-muted">{t('videos.field.description')}</span>
              <textarea
                rows={4}
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                aria-label={t('videos.field.description')}
                data-testid="scene-description-input"
                className={`${input} resize-y`}
              />
            </label>
            <label className="block">
              <span className="text-[11px] text-ink-muted">{t('videos.field.tags')}</span>
              <input
                value={form.tags}
                onChange={(e) => setForm({ ...form, tags: e.target.value })}
                aria-label={t('videos.field.tags')}
                className={input}
              />
            </label>
            <div className="grid grid-cols-2 gap-2">
              <label className="block">
                <span className="text-[11px] text-ink-muted">{t('videos.field.place')}</span>
                <input
                  value={form.place}
                  onChange={(e) => setForm({ ...form, place: e.target.value })}
                  aria-label={t('videos.field.place')}
                  className={input}
                />
              </label>
              <label className="block">
                <span className="text-[11px] text-ink-muted">{t('videos.field.scene_kind')}</span>
                <input
                  value={form.scene_kind}
                  onChange={(e) => setForm({ ...form, scene_kind: e.target.value })}
                  aria-label={t('videos.field.scene_kind')}
                  className={input}
                />
              </label>
            </div>
            <label className="block">
              <span className="text-[11px] text-ink-muted">{t('videos.field.screen_text')}</span>
              <textarea
                rows={2}
                value={form.screen_text}
                onChange={(e) => setForm({ ...form, screen_text: e.target.value })}
                aria-label={t('videos.field.screen_text')}
                className={`${input} resize-y`}
              />
            </label>
            <div className="flex items-center gap-2">
              <OciButton disabled={busy} onClick={() => void save()} data-testid="scene-save">
                {t('videos.detail.save')}
              </OciButton>
              <OciButton
                variant="outline"
                disabled={busy}
                onClick={() => { setForm(toForm(scene)); setEditing(false) }}
              >
                {t('videos.detail.cancel')}
              </OciButton>
            </div>
          </div>
        ) : (
          <div className="space-y-1.5">
            <p className="whitespace-pre-wrap text-sm leading-relaxed" data-testid="scene-description">
              {scene.description}
            </p>
            {asStringList(scene.tags).length > 0 && (
              <div className="flex flex-wrap gap-1">
                {asStringList(scene.tags).map((tag) => (
                  <span
                    key={tag}
                    className="rounded-full border border-line bg-bg px-2 py-0.5 text-[10px] text-ink-muted"
                  >
                    {tag}
                  </span>
                ))}
              </div>
            )}
            {field(t('videos.field.place'), scene.place ?? '')}
            {field(t('videos.field.scene_kind'), scene.scene_kind ?? '')}
            {field(t('videos.field.weather'), scene.weather ?? '')}
            {field(t('videos.field.screen_text'), scene.screen_text ?? '')}
            {field(t('videos.field.objects'), asStringList(scene.objects).join('・'))}
            {field(t('videos.field.actions'), asStringList(scene.actions).join('・'))}
            {people && (
              <p className="text-xs">
                <span className="text-ink-muted">{t('videos.field.people')}: </span>
                {String(people.present ?? '')}
                {people.count != null ? ` (${String(people.count)})` : ''}
              </p>
            )}
            <div className="flex flex-wrap items-center gap-2 pt-1">
              <OciButton
                variant="outline"
                disabled={busy || assetState === 'running'}
                onClick={() => setEditing(true)}
                data-testid="scene-edit"
              >
                {t('videos.detail.edit')}
              </OciButton>
              <OciButton
                variant="outline"
                disabled={busy || assetState === 'running'}
                onClick={() => void confirm()}
              >
                {t('videos.detail.confirm')}
              </OciButton>
              <button
                type="button"
                onClick={() => void toggleHistory()}
                className="text-xs text-action hover:underline"
              >
                {t('videos.detail.history')}
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => void remove()}
                className="ml-auto text-xs text-ink-muted hover:text-primary-strong disabled:opacity-40"
              >
                {t('videos.detail.deleteScene')}
              </button>
            </div>
          </div>
        )}

        {edits && (
          <div className="border-t border-line pt-2">
            <h3 className="text-xs font-bold text-ink-muted">{t('videos.detail.history')}</h3>
            {edits.length === 0 ? (
              <p className="mt-1 text-[11px] text-ink-muted">{t('videos.detail.historyEmpty')}</p>
            ) : (
              <ul className="mt-1 space-y-1">
                {edits.map((e, i) => (
                  <li key={`${e.field}-${e.edited_at}-${i}`} className="text-[11px] leading-snug">
                    <span className="text-ink-muted">{formatUtc(e.edited_at)} </span>
                    <span className="font-medium">
                      {fieldLabel((k) => t(k as Parameters<typeof t>[0]), e.field)}
                    </span>
                    : <span className="line-through opacity-60">{e.before ?? ''}</span>{' → '}
                    <span>{e.after ?? ''}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </Panel>
  )
}
