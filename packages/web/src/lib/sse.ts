/** SSE(Server-Sent Events)ストリームの共通パーサ。§3 / docs/refactoring/review-validation.md
 *
 *  画面ごとに重複していた `res.body.getReader() → TextDecoder → "\n\n"分割 →
 *  "data:"抽出 → [DONE]スキップ → JSON.parse` ループを一本化する。
 *
 *  方針:
 *  - React/auth.tsx非依存（純粋・テスト容易）。authHeadersや reauthenticate は
 *    呼び出し側が opts 経由で注入する（import循環の回避）。
 *  - 9箇所の差分（401の throw/return 2系統・Abortの break/無視 2系統・
 *    イベント型の画面差・parse error方針）は全て opts で吸収する。
 */

/** res.status===401 を検出したときの挙動。
 *  - 'throw'（既定）: onUnauthorized() を呼んだ後 SseUnauthorizedError を throw する。
 *    chat/dbchat/rag/usecase/minutes/video の「reauthenticate(); throw new Error(...)」系。
 *  - 'reauth-return': onUnauthorized() を呼んでストリームを読まず正常 return する。
 *    voicechat#1 / realtime の「return reauthenticate()」系。 */
export type On401Policy = 'throw' | 'reauth-return'

export interface ReadSseOptions {
  /** 401検出時の挙動。既定 'throw'。 */
  on401?: On401Policy
  /** 401検出時に呼ぶコールバック（通常は auth.tsx の reauthenticate を渡す）。
   *  sse.ts は auth.tsx を import しないため、呼び出し側が配線する。 */
  onUnauthorized?: () => void
  /** ストリーム中断用の AbortSignal。fetch() に渡したものと同じものを渡す。 */
  signal?: AbortSignal
  /** true のとき AbortError を握りつぶして正常 return する（voicechatの `catch {}` 相当）。
   *  既定 false（AbortError は rethrow し、呼び出し側で break/無視を判断）。 */
  silentAbort?: boolean
  /** data行のJSON.parseが失敗したときの方針。
   *  - 未指定: そのまま throw（既存の素の JSON.parse と同じ挙動）。
   *  - 'ignore': 不正な行をスキップして処理を続行。
   *  - コールバック: (err, rawLine) を渡して続行（ログ等）。 */
  onParseError?: 'ignore' | ((err: unknown, rawLine: string) => void)
}

/** 401時に on401:'throw' で投げられる識別可能なエラー。 */
export class SseUnauthorizedError extends Error {
  constructor(message = 'Unauthorized (401)') {
    super(message)
    this.name = 'SseUnauthorizedError'
  }
}

function isAbortError(e: unknown): boolean {
  return e instanceof DOMException && e.name === 'AbortError'
}

/**
 * SSEレスポンスを読み、`data:` 行ごとに JSON をパースして onEvent に渡す。
 *
 * 契約:
 * - 呼び出し側は事前に fetch() を済ませ、その Response をそのまま渡す。
 *   res.ok / 503 等のハンドリングは呼び出し側の責務（画面ごとに異なるため）。
 * - 401 はストリームを読む前に検出し、on401 方針に従う。
 * - 完走（done）またはAbort（signal発火）で正常終了する。
 * - parseエラーは onParseError 方針に従う。
 *
 * @typeParam T - data行の JSON 形（画面ごとに指定。例 `{ delta?: string; error?: string }`）。
 */
export async function readSse<T>(
  res: Response,
  onEvent: (ev: T) => void,
  opts: ReadSseOptions = {},
): Promise<void> {
  const { on401 = 'throw', onUnauthorized, signal, silentAbort = false, onParseError } = opts

  // ストリームを読む前に 401 を判定する（呼び出し側の if(res.status===401) 相当を集約）。
  if (res.status === 401) {
    onUnauthorized?.()
    if (on401 === 'reauth-return') return
    throw new SseUnauthorizedError()
  }

  if (!res.body) throw new Error('SSE response has no body')

  const reader = res.body.getReader()
  const dec = new TextDecoder()
  let buf = ''

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += dec.decode(value, { stream: true })
      const parts = buf.split('\n\n')
      buf = parts.pop() ?? ''
      for (const part of parts) {
        const line = part.trim()
        if (!line.startsWith('data:')) continue
        const data = line.slice(5).trim()
        if (data === '[DONE]') continue
        let ev: T
        try {
          ev = JSON.parse(data) as T
        } catch (err) {
          if (onParseError === 'ignore') continue
          if (typeof onParseError === 'function') {
            onParseError(err, data)
            continue
          }
          throw err
        }
        onEvent(ev)
      }
    }
  } catch (e) {
    // AbortControllerでの中断: silentAbort指定時は握りつぶし、それ以外はrethrow。
    if (isAbortError(e)) {
      if (silentAbort) return
      throw e
    }
    throw e
  } finally {
    // 中断時にネットワークを確実に解放する（呼び出し側が abort 済みでも安全）。
    if (signal?.aborted) {
      try {
        await reader.cancel()
      } catch {
        /* 既に閉じている場合は無視 */
      }
    }
  }
}
