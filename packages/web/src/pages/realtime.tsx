/** リアルタイム文字起こし(VOICE-02): マイク→16kHz PCMチャンクPOST→SSEでfinal受信
 *  Whisperリアルタイムはpartialなしのためfinalのみが数秒遅れで届く(SPIKE-06) */
import { useEffect, useRef, useState } from 'react'
import { authHeaders, reauthenticate, useUser } from '../auth'
import { PageContainer } from '../components/layout'
import { readSse } from '../lib/sse'
import { OciButton, Panel, StatusBadge } from '../components/oci'
import { usePrefs } from '../prefs'

type Line = { id: number; at: string; text: string; translated?: string }

// AudioWorklet: 入力フレーム(float32)をそのままメインスレッドへ送る
const WORKLET_CODE = `
class PcmCapture extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0]
    if (ch) this.port.postMessage(ch.slice(0))
    return true
  }
}
registerProcessor('pcm-capture', PcmCapture)
`

const CHUNK_MS = 250

export default function Realtime() {
  const { t } = usePrefs()
  const user = useUser()
  const [running, setRunning] = useState(false)
  const [connecting, setConnecting] = useState(false)
  const [lines, setLines] = useState<Line[]>([])
  const [error, setError] = useState<string | null>(null)
  const [language, setLanguage] = useState('ja')
  // ENH-10: リアルタイム翻訳
  const [translateOn, setTranslateOn] = useState(true)
  const [targetLang, setTargetLang] = useState('en')
  const [transBackend, setTransBackend] = useState('llm')
  const [langOpts, setLangOpts] = useState<{ code: string; label: string }[]>([])
  const [backendOpts, setBackendOpts] = useState<{ name: string; label: string }[]>([])
  const lineIdRef = useRef(0)
  // SSEリーダはstart時に生成され以後の状態変更を捕捉しないため、翻訳設定はrefで参照
  const cfgRef = useRef({ on: true, target: 'en', backend: 'llm', source: 'ja' })
  const ctxRef = useRef<AudioContext | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const sidRef = useRef<string | null>(null)
  const sseAbortRef = useRef<AbortController | null>(null)
  const bufRef = useRef<Int16Array[]>([])
  const flushTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const sendChainRef = useRef<Promise<void>>(Promise.resolve())

  const stop = async () => {
    setRunning(false)
    if (flushTimerRef.current) clearInterval(flushTimerRef.current)
    flushTimerRef.current = null
    sseAbortRef.current?.abort()
    streamRef.current?.getTracks().forEach((tr) => tr.stop())
    streamRef.current = null
    await ctxRef.current?.close().catch(() => undefined)
    ctxRef.current = null
    const sid = sidRef.current
    sidRef.current = null
    if (sid) {
      fetch(`/api/stt/sessions/${sid}`, { method: 'DELETE', headers: authHeaders(user) }).catch(
        () => undefined,
      )
    }
  }

  useEffect(() => () => void stop(), []) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    fetch('/api/translate/options', { headers: authHeaders(user) })
      .then((r) => r.json())
      .then((d) => {
        setLangOpts(d.languages ?? [])
        setBackendOpts(d.backends ?? [])
      })
      .catch(() => undefined)
  }, [user])

  useEffect(() => {
    cfgRef.current = { on: translateOn, target: targetLang, backend: transBackend, source: language }
  }, [translateOn, targetLang, transBackend, language])

  const translateLine = async (id: number, text: string) => {
    const cfg = cfgRef.current
    try {
      const res = await fetch('/api/translate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
        body: JSON.stringify({ text, target: cfg.target, source: cfg.source, backend: cfg.backend }),
      })
      if (!res.ok) return
      const { translated } = await res.json()
      setLines((ls) => ls.map((l) => (l.id === id ? { ...l, translated } : l)))
    } catch {
      /* 翻訳失敗は原文表示のまま(致命的でない) */
    }
  }

  const readEvents = async (sid: string, ac: AbortController) => {
    try {
      const res = await fetch(`/api/stt/sessions/${sid}/events`, {
        headers: authHeaders(user),
        signal: ac.signal,
      })
      if (res.status === 401) return reauthenticate()
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)
      await readSse<{ text?: string; is_final?: boolean; error?: string; closed?: boolean }>(
        res,
        (ev) => {
          if (ev.text && ev.is_final) {
            const at = new Date().toLocaleTimeString('ja-JP', { hour12: false })
            const id = ++lineIdRef.current
            const text = ev.text
            setLines((ls) => [...ls, { id, at, text }])
            if (cfgRef.current.on && cfgRef.current.target !== cfgRef.current.source)
              void translateLine(id, text)
          }
          if (ev.error) setError(ev.error)
          if (ev.closed && sidRef.current === sid) void stop()
        },
        { signal: ac.signal },
      )
    } catch (e) {
      const aborted = e instanceof DOMException && e.name === 'AbortError'
      if (!aborted) setError(String(e instanceof Error ? e.message : e))
    }
  }

  const flush = (sid: string) => {
    const chunks = bufRef.current
    bufRef.current = []
    if (chunks.length === 0) return
    const total = chunks.reduce((n, c) => n + c.length, 0)
    const merged = new Int16Array(total)
    let off = 0
    for (const c of chunks) {
      merged.set(c, off)
      off += c.length
    }
    // 送信順を保証するため直列化(チェーン)
    sendChainRef.current = sendChainRef.current.then(() =>
      fetch(`/api/stt/sessions/${sid}/audio`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream', ...authHeaders(user) },
        body: merged.buffer as ArrayBuffer,
      })
        .then((r) => {
          if (r.status === 404 && sidRef.current === sid) void stop()
        })
        .catch(() => undefined),
    )
  }

  const start = async () => {
    if (running || connecting) return
    setError(null)
    setConnecting(true)
    try {
      // 1. マイク(16kHzはAudioContext側でリサンプル)
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      streamRef.current = stream
      const ctx = new AudioContext({ sampleRate: 16000 })
      ctxRef.current = ctx
      const url = URL.createObjectURL(new Blob([WORKLET_CODE], { type: 'application/javascript' }))
      await ctx.audioWorklet.addModule(url)
      URL.revokeObjectURL(url)

      // 2. サーバーセッション(OCIリアルタイムWS)
      const res = await fetch('/api/stt/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
        body: JSON.stringify({ language }),
      })
      if (res.status === 401) return reauthenticate()
      const data = await res.json()
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`)
      const sid: string = data.id
      sidRef.current = sid

      // 3. 結果SSE購読
      const ac = new AbortController()
      sseAbortRef.current = ac
      void readEvents(sid, ac)

      // 4. 取り込み開始(float32→int16、CHUNK_MSごとにPOST)
      const src = ctx.createMediaStreamSource(stream)
      const node = new AudioWorkletNode(ctx, 'pcm-capture')
      node.port.onmessage = (e: MessageEvent<Float32Array>) => {
        const f = e.data
        const pcm = new Int16Array(f.length)
        for (let i = 0; i < f.length; i++) {
          const v = Math.max(-1, Math.min(1, f[i]))
          pcm[i] = v < 0 ? v * 0x8000 : v * 0x7fff
        }
        bufRef.current.push(pcm)
      }
      src.connect(node)
      flushTimerRef.current = setInterval(() => flush(sid), CHUNK_MS)
      setRunning(true)
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e))
      await stop()
    } finally {
      setConnecting(false)
    }
  }

  return (
    <PageContainer icon="realtime" title={t('nav.realtime')} subtitle={t('rt.lead')} helpKey="realtime">
      <Panel
        title={t('rt.title')}
        action={
          <div className="flex items-center gap-2 text-xs">
            <label className="flex items-center gap-1">
              <span className="text-ink-muted">{t('rt.sourceLang')}</span>
              <select
                value={language}
                onChange={(e) => setLanguage(e.target.value)}
                disabled={running || connecting}
                className="rounded-rw border border-line bg-surface px-2 py-1"
                title={t('rt.sourceLang')}
              >
                <option value="ja">日本語</option>
                <option value="en">English</option>
              </select>
            </label>
            {running ? (
              <>
                <StatusBadge kind="ok">{t('rt.recording')}</StatusBadge>
                <OciButton variant="outline" onClick={() => void stop()}>
                  {t('rt.stop')}
                </OciButton>
              </>
            ) : (
              <OciButton onClick={() => void start()} disabled={connecting}>
                {connecting ? t('rt.connecting') : t('rt.start')}
              </OciButton>
            )}
            {lines.length > 0 && !running && (
              <OciButton
                variant="ghost"
                onClick={() => navigator.clipboard.writeText(lines.map((l) => l.text).join('\n'))}
              >
                {t('chat.copy')}
              </OciButton>
            )}
          </div>
        }
      >
        {error && (
          <div className="mb-3 rounded-rw bg-pill-err px-3 py-2 text-sm text-pill-err-ink">
            {error}
          </div>
        )}
        {/* 翻訳設定(ENH-10) */}
        <div className="mb-3 flex flex-wrap items-center gap-x-5 gap-y-2 border-b border-line pb-3 text-sm">
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={translateOn}
              onChange={(e) => setTranslateOn(e.target.checked)}
            />
            {t('rt.translate')}
          </label>
          <label className={`flex items-center gap-2 ${translateOn ? '' : 'opacity-40'}`}>
            <span className="text-ink-muted">{t('rt.target')}</span>
            <select
              value={targetLang}
              disabled={!translateOn}
              onChange={(e) => setTargetLang(e.target.value)}
              className="rounded-rw border border-line bg-surface px-2 py-1 text-xs"
            >
              {langOpts.map((l) => (
                <option key={l.code} value={l.code}>{l.label}</option>
              ))}
            </select>
          </label>
          <label className={`flex items-center gap-2 ${translateOn ? '' : 'opacity-40'}`}>
            <span className="text-ink-muted">{t('rt.backend')}</span>
            <select
              value={transBackend}
              disabled={!translateOn}
              onChange={(e) => setTransBackend(e.target.value)}
              className="rounded-rw border border-line bg-surface px-2 py-1 text-xs"
            >
              {backendOpts.map((b) => (
                <option key={b.name} value={b.name}>{b.label}</option>
              ))}
            </select>
          </label>
        </div>
        <p className="mb-3 text-[11px] text-ink-muted">{t('rt.note')}</p>
        {lines.length === 0 ? (
          <p className="text-xs text-ink-muted">{running ? t('rt.listening') : t('rt.hint')}</p>
        ) : (
          <div className="max-h-[28rem] space-y-2 overflow-y-auto">
            {lines.map((l) => (
              <div key={l.id} className="flex items-start gap-2 text-sm">
                <span className="mt-0.5 shrink-0 text-[11px] tabular-nums text-ink-muted">
                  {l.at}
                </span>
                <span className="min-w-0">
                  <span className="text-ink-muted">{l.text}</span>
                  {translateOn && (
                    <span className="mt-0.5 block font-medium text-ink">
                      {l.translated ?? '…'}
                    </span>
                  )}
                </span>
              </div>
            ))}
          </div>
        )}
      </Panel>
    </PageContainer>
  )
}
