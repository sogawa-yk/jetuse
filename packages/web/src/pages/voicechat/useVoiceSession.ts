/** 音声チャットの録音+STTセッション(voicechat.tsx分割: review-validation.md §7)。
 *  マイク(16kHz PCM)をCHUNK_MSごとにPOSTし、SSEで final テキストを受信する(VOICE-02基盤を再利用)。
 *  STT SSE は abort/セッション断を握りつぶす(silentAbort): 断は次回録音時に再作成される。 */
import { useRef, useState } from 'react'
import { authHeaders, reauthenticate, type User } from '../../auth'
import { readSse } from '../../lib/sse'

const CHUNK_MS = 250

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

export type UseVoiceSessionArgs = {
  user: User
  /** 401時の i18n メッセージ(uc.sessionLost)。 */
  t: (key: 'uc.sessionLost') => string
}

export function useVoiceSession({ user, t }: UseVoiceSessionArgs) {
  const [partialUser, setPartialUser] = useState('')

  const ctxRef = useRef<AudioContext | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const sidRef = useRef<string | null>(null)
  const sseAbortRef = useRef<AbortController | null>(null)
  const bufRef = useRef<Int16Array[]>([])
  const flushTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const sendChainRef = useRef<Promise<void>>(Promise.resolve())
  const finalsRef = useRef<string[]>([])

  const cleanupMic = async () => {
    if (flushTimerRef.current) clearInterval(flushTimerRef.current)
    flushTimerRef.current = null
    streamRef.current?.getTracks().forEach((tr) => tr.stop())
    streamRef.current = null
    await ctxRef.current?.close().catch(() => undefined)
    ctxRef.current = null
  }

  const closeSession = () => {
    sseAbortRef.current?.abort()
    const sid = sidRef.current
    sidRef.current = null
    if (sid) {
      fetch(`/api/stt/sessions/${sid}`, { method: 'DELETE', headers: authHeaders(user) }).catch(
        () => undefined,
      )
    }
  }

  const readSttEvents = async (sid: string, ac: AbortController) => {
    try {
      const res = await fetch(`/api/stt/sessions/${sid}/events`, {
        headers: authHeaders(user),
        signal: ac.signal,
      })
      if (res.status === 401) return reauthenticate()
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)
      await readSse<{ text?: string; is_final?: boolean; closed?: boolean }>(
        res,
        (ev) => {
          if (ev.text && ev.is_final) {
            finalsRef.current.push(ev.text)
            setPartialUser(finalsRef.current.join(''))
          }
          if (ev.closed && sidRef.current === sid) sidRef.current = null
        },
        { signal: ac.signal, silentAbort: true },
      )
    } catch {
      /* abort時は無視。セッション断は次回録音時に再作成される */
    }
  }

  const ensureSession = async (): Promise<string> => {
    if (sidRef.current) return sidRef.current
    const res = await fetch('/api/stt/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
      body: JSON.stringify({ language: 'ja' }),
    })
    if (res.status === 401) {
      reauthenticate()
      throw new Error(t('uc.sessionLost'))
    }
    const data = await res.json()
    if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`)
    sidRef.current = data.id
    const ac = new AbortController()
    sseAbortRef.current = ac
    void readSttEvents(data.id, ac)
    return data.id
  }

  const postAudio = (sid: string, body: ArrayBuffer) => {
    sendChainRef.current = sendChainRef.current.then(() =>
      fetch(`/api/stt/sessions/${sid}/audio`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream', ...authHeaders(user) },
        body,
      })
        .then((r) => {
          if (r.status === 404) sidRef.current = null
        })
        .catch(() => undefined),
    )
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
    postAudio(sid, merged.buffer as ArrayBuffer)
  }

  /** マイクを開き、CHUNK_MSごとにPCMをPOSTし始める。 */
  const startRecording = async () => {
    setPartialUser('')
    finalsRef.current = []
    const sid = await ensureSession()
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    streamRef.current = stream
    const ctx = new AudioContext({ sampleRate: 16000 })
    ctxRef.current = ctx
    const url = URL.createObjectURL(new Blob([WORKLET_CODE], { type: 'application/javascript' }))
    await ctx.audioWorklet.addModule(url)
    URL.revokeObjectURL(url)
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
  }

  /** マイクを止め、無音1秒で発話区切りを促し final到着を最大5秒待ってから確定テキストを返す。 */
  const finalize = async (): Promise<string> => {
    const sid = sidRef.current
    await cleanupMic()
    if (sid) {
      // 無音1秒で発話区切りを促し(specs/12)、final到着を最大5秒待つ
      postAudio(sid, new Int16Array(16000).buffer as ArrayBuffer)
      const before = finalsRef.current.length
      const t0 = Date.now()
      while (Date.now() - t0 < 5000) {
        await new Promise((r) => setTimeout(r, 250))
        if (finalsRef.current.length > before && Date.now() - t0 > 1500) break
      }
    }
    const text = finalsRef.current.join('')
    setPartialUser('')
    return text
  }

  const cleanup = () => {
    void cleanupMic()
    closeSession()
  }

  return { partialUser, startRecording, finalize, cleanup }
}
