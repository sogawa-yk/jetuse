/** 音声チャットのTTS再生(voicechat.tsx分割: review-validation.md §7)。
 *  センテンス単位で /api/tts を先行発行(パイプライン)し、playLoop が順序保証して再生する。
 *  generation 番号で停止時に古い再生キューを無効化する(VOICE-03)。 */
import { useRef, useState } from 'react'
import { authHeaders, type User } from '../../auth'

export type Voice = string

export function useTtsPlayback(user: User) {
  const [speaking, setSpeaking] = useState(false)
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const playQueueRef = useRef<Promise<Blob | null>[]>([])
  const playingRef = useRef(false)
  const generationRef = useRef(0) // 停止時に古い再生キューを無効化

  const playLoop = async (gen: number) => {
    if (playingRef.current) return
    playingRef.current = true
    setSpeaking(true)
    try {
      while (playQueueRef.current.length > 0 && generationRef.current === gen) {
        const blob = await playQueueRef.current.shift()!
        if (!blob || generationRef.current !== gen) continue
        const url = URL.createObjectURL(blob)
        const audio = new Audio(url)
        audioRef.current = audio
        await audio.play().catch(() => undefined)
        await new Promise<void>((resolve) => {
          audio.onended = () => resolve()
          audio.onerror = () => resolve()
          audio.onpause = () => resolve()
        })
        URL.revokeObjectURL(url)
      }
    } finally {
      playingRef.current = false
      setSpeaking(false)
      audioRef.current = null
    }
  }

  /** 現在の generation を返す(ask 開始時に固定して enqueue/再生の世代を揃える)。 */
  const currentGen = () => generationRef.current

  const enqueueTts = (sentence: string, gen: number, voice: Voice, enabled: boolean) => {
    if (!enabled) return
    // fetchを先行発行(パイプライン)。再生はplayLoopが順序保証
    const p = fetch('/api/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
      body: JSON.stringify({ text: sentence.slice(0, 500), voice }),
    })
      .then((r) => (r.ok ? r.blob() : null))
      .catch(() => null)
    playQueueRef.current.push(p)
    void playLoop(gen)
  }

  /** 再生を停止し、世代を進めて以後のキューを無効化する。 */
  const stopPlayback = () => {
    generationRef.current++
    playQueueRef.current = []
    audioRef.current?.pause()
    audioRef.current = null
    setSpeaking(false)
  }

  return { speaking, currentGen, enqueueTts, stopPlayback }
}
