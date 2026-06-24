/** 音声チャットv1(VOICE-03): 半二重(話す→停止→STT final結合→LLM→TTS順次再生)。
 *  録音+STTは useVoiceSession、TTS再生は useTtsPlayback に分割(review-validation.md §7)。 */
import { useEffect, useRef, useState } from 'react'
import { authHeaders, reauthenticate, useUser } from '../auth'
import { PageContainer } from '../components/layout'
import { OciButton, Panel, StatusBadge } from '../components/oci'
import { readSse } from '../lib/sse'
import { usePrefs } from '../prefs'
import { useTtsPlayback } from './voicechat/useTtsPlayback'
import { useVoiceSession } from './voicechat/useVoiceSession'

type Msg = { role: 'user' | 'assistant'; content: string }
type Phase = 'idle' | 'recording' | 'finalizing' | 'answering'

// 日本語TTSボイス全5種(Phoenix list_voicesで確認、2026-06-12)
const VOICES = [
  { id: 'Yuki', label: 'Yuki（女性）' },
  { id: 'Aiko', label: 'Aiko（女性）' },
  { id: 'Hana', label: 'Hana（女性）' },
  { id: 'Sakura', label: 'Sakura（女性）' },
  { id: 'Satoshi', label: 'Satoshi（男性）' },
] as const
// 音声向け: 読み上げ前提の話し言葉を指示(specs/12 VOICE-03)
const VOICE_SYSTEM_PROMPT =
  '音声チャットです。回答は読み上げられるため、話し言葉で簡潔に答えてください。' +
  '記号・箇条書き・コードブロック・マークダウンは使わず、2〜3文程度にまとめてください。'

const splitSentences = (buf: string): { done: string[]; rest: string } => {
  const done: string[] = []
  let rest = buf
  for (;;) {
    const m = rest.match(/[。．！？!?\n]/)
    if (!m || m.index === undefined) break
    const sentence = rest.slice(0, m.index + 1).trim()
    rest = rest.slice(m.index + 1)
    if (sentence) done.push(sentence)
  }
  return { done, rest }
}

export default function VoiceChat() {
  const { t } = usePrefs()
  const user = useUser()
  const [phase, setPhase] = useState<Phase>('idle')
  const [messages, setMessages] = useState<Msg[]>([])
  const [error, setError] = useState<string | null>(null)
  const [voice, setVoice] = useState<(typeof VOICES)[number]['id']>('Yuki')
  const [speak, setSpeak] = useState(true)

  const session = useVoiceSession({ user, t })
  const tts = useTtsPlayback(user)
  const chatAbortRef = useRef<AbortController | null>(null)

  const stopAll = () => {
    tts.stopPlayback()
    chatAbortRef.current?.abort()
    session.cleanup()
    setPhase('idle')
  }

  useEffect(() => () => stopAll(), []) // eslint-disable-line react-hooks/exhaustive-deps

  const startRecording = async () => {
    if (phase !== 'idle') return
    setError(null)
    try {
      await session.startRecording()
      setPhase('recording')
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e))
      stopAll()
    }
  }

  const stopAndSend = async () => {
    if (phase !== 'recording') return
    setPhase('finalizing')
    const text = await session.finalize()
    if (!text.trim()) {
      setError(t('vc.noSpeech'))
      setPhase('idle')
      return
    }
    await ask(text.trim())
  }

  const ask = async (text: string) => {
    setPhase('answering')
    const gen = tts.currentGen()
    const history: Msg[] = [...messages, { role: 'user', content: text }]
    setMessages([...history, { role: 'assistant', content: '' }])
    const ac = new AbortController()
    chatAbortRef.current = ac
    let acc = ''
    let pending = ''
    try {
      const res = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
        body: JSON.stringify({
          model: 'gpt-oss-120b',
          source: 'voicechat',
          messages: [{ role: 'system', content: VOICE_SYSTEM_PROMPT }, ...history],
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
          if (ev.error) setError(ev.error)
          if (!ev.delta) return
          acc += ev.delta
          pending += ev.delta
          const { done: sentences, rest } = splitSentences(pending)
          pending = rest
          for (const s of sentences) tts.enqueueTts(s, gen, voice, speak)
          setMessages((ms) => {
            const next = [...ms]
            next[next.length - 1] = { role: 'assistant', content: acc }
            return next
          })
        },
        { signal: ac.signal },
      )
      if (pending.trim()) tts.enqueueTts(pending.trim(), gen, voice, speak)
    } catch (e) {
      const aborted = e instanceof DOMException && e.name === 'AbortError'
      if (!aborted) setError(String(e instanceof Error ? e.message : e))
    } finally {
      chatAbortRef.current = null
      setPhase('idle')
    }
  }

  const PHASE_LABEL: Record<Phase, string> = {
    idle: '',
    recording: t('vc.recording'),
    finalizing: t('vc.finalizing'),
    answering: t('vc.answering'),
  }

  return (
    <PageContainer icon="voicechat" title={t('nav.voicechat')} subtitle={t('vc.lead')} helpKey="voicechat">
      <Panel
        title={t('vc.title')}
        action={
          phase === 'idle' && !tts.speaking ? (
            <OciButton onClick={() => void startRecording()}>{t('vc.talk')}</OciButton>
          ) : phase === 'recording' ? (
            <OciButton onClick={() => void stopAndSend()}>{t('vc.send')}</OciButton>
          ) : (
            <OciButton variant="outline" onClick={stopAll}>
              {t('chat.stop')}
            </OciButton>
          )
        }
      >
        {/* 設定(主操作から分離 — ENH-08): 読み上げトグル + ボイス選択 */}
        <div className="mb-3 flex flex-wrap items-center gap-x-6 gap-y-2 border-b border-line pb-3 text-sm">
          <div className="flex items-center gap-2">
            <span className="text-ink-muted">{t('vc.speak')}</span>
            <button
              type="button"
              role="switch"
              aria-checked={speak}
              onClick={() => setSpeak((v) => !v)}
              className={`relative h-5 w-9 rounded-full transition-colors ${
                speak ? 'bg-action' : 'bg-line'
              }`}
            >
              <span
                className={`absolute top-0.5 h-4 w-4 rounded-full bg-surface shadow transition-all ${
                  speak ? 'left-[18px]' : 'left-0.5'
                }`}
              />
            </button>
            <span className="text-xs text-ink-muted">{speak ? 'ON' : 'OFF'}</span>
          </div>
          <label
            className={`flex items-center gap-2 ${speak ? '' : 'opacity-40'}`}
          >
            <span className="text-ink-muted">{t('vc.voice')}</span>
            <select
              value={voice}
              disabled={!speak}
              onChange={(e) => setVoice(e.target.value as (typeof VOICES)[number]['id'])}
              className="rounded-rw border border-line bg-surface px-2 py-1 text-xs disabled:cursor-not-allowed"
            >
              {VOICES.map((v) => (
                <option key={v.id} value={v.id}>
                  {v.label}
                </option>
              ))}
            </select>
          </label>
        </div>
        {error && (
          <div className="mb-3 rounded-rw bg-pill-err px-3 py-2 text-sm text-pill-err-ink">
            {error}
          </div>
        )}
        <div className="mb-3 flex items-center gap-2 text-xs text-ink-muted">
          {PHASE_LABEL[phase] && <StatusBadge kind="warn">{PHASE_LABEL[phase]}</StatusBadge>}
          {tts.speaking && <StatusBadge kind="ok">{t('vc.speaking')}</StatusBadge>}
          {!PHASE_LABEL[phase] && !tts.speaking && <span>{t('vc.hint')}</span>}
        </div>
        <div className="max-h-[28rem] space-y-3 overflow-y-auto">
          {messages.map((m, i) => (
            <div key={i} className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
              <div
                className={`max-w-[80%] whitespace-pre-wrap rounded-rw px-4 py-2.5 text-sm ${
                  m.role === 'user'
                    ? 'rounded-tr-none bg-band text-band-ink'
                    : 'rounded-tl-none border border-line bg-surface'
                }`}
              >
                {m.content || '…'}
              </div>
            </div>
          ))}
          {session.partialUser && (
            <div className="flex justify-end">
              <div className="max-w-[80%] rounded-rw rounded-tr-none bg-band/60 px-4 py-2.5 text-sm text-band-ink">
                {session.partialUser}
              </div>
            </div>
          )}
        </div>
      </Panel>
    </PageContainer>
  )
}
