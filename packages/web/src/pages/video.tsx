/** 映像分析(MM-01): 動画→等間隔Nフレーム抽出(ブラウザ内canvas)→visionモデルで一括分析。
 *  動画自体はサーバーへ送らない(フレームのみ /api/chat/stream のimagesで送信) */
import { useEffect, useRef, useState } from 'react'
import { authHeaders, reauthenticate, useUser } from '../auth'
import { PageContainer } from '../components/layout'
import { Md } from '../components/markdown'
import { readSse } from '../lib/sse'
import { OciButton, Panel } from '../components/oci'
import { usePrefs } from '../prefs'

type ModelInfo = { key: string; label: string; vision?: boolean; multi_image?: boolean }

const FRAME_COUNTS = [4, 6, 8, 10]
const MAX_FRAME_PX = 768

export default function Video() {
  const { t } = usePrefs()
  const user = useUser()
  const [models, setModels] = useState<ModelInfo[]>([])
  const [model, setModel] = useState('gemini-2.5-flash')
  const [frameCount, setFrameCount] = useState(6)
  const [frames, setFrames] = useState<string[]>([])
  const [videoName, setVideoName] = useState('')
  const [extracting, setExtracting] = useState(false)
  const [prompt, setPrompt] = useState('')
  const [output, setOutput] = useState('')
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  useEffect(() => {
    fetch('/api/chat/models', { headers: authHeaders(user) })
      .then((r) => r.json())
      // 複数フレームを送るため multi_image 対応モデルのみ(ENH-09: llama-3.2-visionは1枚制限で400)
      .then((d) => setModels((d.models ?? []).filter((m: ModelInfo) => m.vision && m.multi_image)))
      .catch(() => setModels([]))
  }, [user])

  const extractFrames = async (file: File) => {
    setExtracting(true)
    setError(null)
    setFrames([])
    setOutput('')
    setVideoName(file.name)
    const url = URL.createObjectURL(file)
    try {
      const video = document.createElement('video')
      video.preload = 'auto'
      video.muted = true
      video.src = url
      await new Promise<void>((resolve, reject) => {
        video.onloadedmetadata = () => resolve()
        video.onerror = () => reject(new Error(t('video.loadError')))
      })
      const duration = video.duration
      if (!isFinite(duration) || duration <= 0) throw new Error(t('video.loadError'))
      const canvas = document.createElement('canvas')
      const scale = Math.min(1, MAX_FRAME_PX / Math.max(video.videoWidth, video.videoHeight))
      canvas.width = Math.round(video.videoWidth * scale)
      canvas.height = Math.round(video.videoHeight * scale)
      const ctx = canvas.getContext('2d')!
      const result: string[] = []
      for (let i = 0; i < frameCount; i++) {
        // 等間隔(区間中央)でシーク
        const at = (duration * (i + 0.5)) / frameCount
        await new Promise<void>((resolve, reject) => {
          video.onseeked = () => resolve()
          video.onerror = () => reject(new Error(t('video.loadError')))
          video.currentTime = at
        })
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height)
        result.push(canvas.toDataURL('image/jpeg', 0.8))
        setFrames([...result])
      }
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e))
    } finally {
      URL.revokeObjectURL(url)
      setExtracting(false)
    }
  }

  const run = async () => {
    if (frames.length === 0 || running) return
    setRunning(true)
    setError(null)
    setOutput('')
    const ac = new AbortController()
    abortRef.current = ac
    const text =
      (prompt.trim() || t('video.defaultPrompt')) +
      `\n\n(添付画像は動画「${videoName}」から等間隔に抽出した${frames.length}フレームです。時系列順に並んでいます)`
    try {
      const res = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
        body: JSON.stringify({
          model,
          source: 'video',
          messages: [{ role: 'user', content: text }],
          images: frames,
        }),
        signal: ac.signal,
      })
      if (res.status === 401) {
        reauthenticate()
        throw new Error(t('uc.sessionLost'))
      }
      if (!res.ok || !res.body) {
        const data = await res.json().catch(() => null)
        throw new Error(
          data && typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`,
        )
      }
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
      setRunning(false)
      abortRef.current = null
    }
  }

  return (
    <PageContainer icon="video" title={t('nav.video')} subtitle={t('video.lead')} wide helpKey="video">
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Panel title={t('video.input')}>
          <div className="space-y-3 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <input
                type="file"
                accept="video/*"
                onChange={(e) => {
                  const f = e.target.files?.[0]
                  if (f) void extractFrames(f)
                  e.target.value = ''
                }}
                className="text-xs file:mr-3 file:rounded-rw file:border-0 file:bg-cta file:px-3 file:py-1.5 file:text-xs file:font-medium file:text-cta-ink"
              />
              <label className="ml-auto flex items-center gap-1 text-xs text-ink-muted">
                {t('video.frames')}
                <select
                  value={frameCount}
                  onChange={(e) => setFrameCount(Number(e.target.value))}
                  disabled={extracting}
                  className="rounded-rw border border-line bg-surface px-2 py-1"
                >
                  {FRAME_COUNTS.map((n) => (
                    <option key={n} value={n}>
                      {n}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <p className="text-[11px] text-ink-muted">{t('video.formats')}</p>
            <p className="text-[11px] text-ink-muted">{t('video.note')}</p>
            {extracting && <p className="text-xs text-ink-muted">{t('video.extracting')}</p>}
            {frames.length > 0 && (
              <div className="grid grid-cols-3 gap-1.5">
                {frames.map((f, i) => (
                  <img key={i} src={f} alt={`frame ${i + 1}`} className="w-full rounded-rw" />
                ))}
              </div>
            )}
            <textarea
              rows={2}
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder={t('video.defaultPrompt')}
              className="w-full resize-none rounded-rw border border-line bg-surface px-3 py-2 text-sm outline-none focus:border-action"
            />
            <div className="flex items-center gap-2">
              <select
                value={model}
                onChange={(e) => setModel(e.target.value)}
                className="rounded-rw border border-line bg-surface px-2 py-1.5 text-xs"
              >
                {models.map((m) => (
                  <option key={m.key} value={m.key}>
                    {m.label}
                  </option>
                ))}
              </select>
              {running ? (
                <OciButton variant="outline" onClick={() => abortRef.current?.abort()}>
                  {t('chat.stop')}
                </OciButton>
              ) : (
                <OciButton onClick={() => void run()} disabled={frames.length === 0 || extracting}>
                  {t('video.analyze')}
                </OciButton>
              )}
            </div>
          </div>
        </Panel>
        <Panel
          title={t('video.result')}
          action={
            output && !running ? (
              <OciButton variant="ghost" onClick={() => navigator.clipboard.writeText(output)}>
                {t('chat.copy')}
              </OciButton>
            ) : undefined
          }
        >
          {error && (
            <div className="mb-3 rounded-rw bg-pill-err px-3 py-2 text-sm text-pill-err-ink">
              {error}
            </div>
          )}
          {output ? (
            <div className="md text-sm">
              <Md>{output}</Md>
            </div>
          ) : (
            <p className="text-xs text-ink-muted">
              {running ? t('video.running') : t('video.hint')}
            </p>
          )}
        </Panel>
      </div>
    </PageContainer>
  )
}
