/** Typed Action Client — `answer.with-citations@1` 専用の薄い型付け境界（EXB-05）。
 *
 *  生成 UI が Action を型安全に消費するためのクライアント。UI コンポーネントに
 *  **生の API URL/パスを露出しない**（実装方針 §11.1）: パスはこのモジュール内部で
 *  組み立て、UI は論理名（experienceId + capability）と入力だけを渡す。
 *
 *  - 呼ぶ API は EXB-03 の Run API（POST .../actions/{actionId}/runs・GET /runs/{id}/events SSE）。
 *  - 型は Stage 0 スキーマ（answer-with-citations.input/output/event ＋ run-event）に対応。
 *    単一の真実源はスキーマ側。ここは手写し最小のミラー。
 *  - SSE 解析は既存の readSse を再利用（重複しない）。callback→AsyncIterable の橋渡しだけ足す。
 *  - auth は readSse と同じく注入式（このモジュールは auth.tsx を import しない＝循環回避・テスト容易）。
 *
 *  MVP は `answer.with-citations@1` 専用（非ゴール: 汎用 SDK / 複数 Action / コード生成）。
 */

import { readSse } from './sse'

// --- Stage 0 契約のミラー型（単一の真実源はスキーマ）-----------------------------

/** answer.with-citations@1 inputSchema */
export interface AnswerInput {
  question: string
  conversationId?: string
}

/** answer.with-citations@1 の引用（output/event 共通） */
export interface Citation {
  source: string
  score?: number
  snippet?: string
}

/** answer.with-citations@1 outputSchema */
export interface AnswerOutput {
  answer: string
  citations: Citation[]
}

/** `answer.with-citations@1` の Run が **実際に発行する** 標準 Run イベントの型。
 *  run-event.schema.json の語彙のうち本 capability が出すもの（+ lifecycle 終端）に、
 *  固有 data を載せる。標準語彙全体（tool.started/tool.completed/approval.required/artifact.created
 *  等）の網羅型ではなく、この Action が観測するイベントに限定したミラー（単一の真実源はスキーマ側）。
 *  SSE は完全な RunEvent（run_id/type/seq/ts/data）を配信するので、それをそのまま型付ける。 */
interface RunEventBase {
  run_id: string
  seq: number
  ts: string
}
export type RunEvent =
  | (RunEventBase & { type: 'run.started'; data: Record<string, never> })
  | (RunEventBase & { type: 'retrieval.started'; data: Record<string, never> })
  | (RunEventBase & { type: 'retrieval.completed'; data: { citations: Citation[] } })
  | (RunEventBase & { type: 'message.delta'; data: { text: string } })
  | (RunEventBase & { type: 'run.completed'; data: { output: AnswerOutput } })
  | (RunEventBase & { type: 'run.failed'; data: { error: string; code: string } })
  | (RunEventBase & { type: 'run.cancelled'; data: Record<string, unknown> })

// --- クライアント -------------------------------------------------------------

export interface JetUseActionClientOptions {
  /** Experience の論理 ID（URL ではない）。パス組み立てはクライアント内部の責務。 */
  experienceId: string
  /** 認証ヘッダの注入（通常は auth.tsx の authHeaders(user) を渡す）。
   *  readSse と同様、このモジュールは auth.tsx を import しない。 */
  headers?: () => Record<string, string>
  /** テスト用の fetch 差し替え。既定はグローバル fetch。 */
  fetchImpl?: typeof fetch
}

/** MVP は単一 capability 専用。UI は生パスでなくこの論理名でクライアントを得る。 */
const ACTION_ID = 'answer.with-citations@1'

export interface JetUseActionClient {
  /** Run を開始し run_id を返す。 */
  start(input: AnswerInput): Promise<{ runId: string }>
  /** 標準 RunEvent を SSE で順に購読する（終端 run.completed/failed で終了）。 */
  events(runId: string): AsyncIterable<RunEvent>
  /** 開始＋購読＋集約の一体型。delta を結合し最終 output を返す（run.failed なら throw）。
   *  onDelta で逐次テキストを受け取れる。 */
  answer(input: AnswerInput, onDelta?: (text: string) => void): Promise<AnswerOutput>
}

/** `answer.with-citations@1` 専用クライアントを生成する。生 API URL は UI に渡らない。 */
export function createAnswerClient(opts: JetUseActionClientOptions): JetUseActionClient {
  const { experienceId, headers, fetchImpl = fetch } = opts
  // パスはここだけで組み立てる（UI 非露出）。encodeURIComponent で論理 ID を安全に埋める。
  const runsPath = `/api/v1/experiences/${encodeURIComponent(experienceId)}/actions/${encodeURIComponent(ACTION_ID)}/runs`
  const eventsPath = (runId: string) => `/api/v1/runs/${encodeURIComponent(runId)}/events`

  async function start(input: AnswerInput): Promise<{ runId: string }> {
    // 信頼境界の入力早期検証（サーバも 422 で弾くが、明快なエラーで fail-fast）。
    if (!input.question) throw new Error('question is required')
    const res = await fetchImpl(runsPath, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...headers?.() },
      body: JSON.stringify(input),
    })
    if (!res.ok) throw await httpError(res)
    const body = (await res.json()) as { run_id?: string }
    if (!body.run_id) throw new Error('run start response missing run_id')
    return { runId: body.run_id }
  }

  async function* events(runId: string): AsyncGenerator<RunEvent> {
    // 購読ごとに abort 用シグナルを持ち、消費側の早期 break/完了/例外で必ず fetch reader を
    // cancel する（＝サーバ側 SSE 購読枠を解放。放置すると _MAX_SUBSCRIBERS を枯渇させる）。
    const ac = new AbortController()
    try {
      const res = await fetchImpl(eventsPath(runId), {
        headers: { ...headers?.() },
        signal: ac.signal,
      })
      if (!res.ok) throw await httpError(res)
      for await (const frame of sseToAsyncIterable<unknown>(res, ac.signal)) {
        // Run API は待機中に keepalive フレーム（`{"ka":1}`）を送る。RunEvent エンベロープを
        // 持たない値は公開型契約から除外する（trust boundary: Provider/GW 由来のノイズも弾く）。
        if (isRunEvent(frame)) yield frame
      }
    } finally {
      ac.abort()
    }
  }

  async function answer(
    input: AnswerInput,
    onDelta?: (text: string) => void,
  ): Promise<AnswerOutput> {
    const { runId } = await start(input)
    let acc = ''
    for await (const ev of events(runId)) {
      switch (ev.type) {
        case 'message.delta':
          acc += ev.data.text
          onDelta?.(ev.data.text)
          break
        case 'run.completed':
          return ev.data.output
        case 'run.failed':
          throw new Error(ev.data.error || 'run failed')
        case 'run.cancelled':
          throw new Error('run cancelled')
      }
    }
    // 終端イベントなしでストリームが閉じた（想定外）。集約分を含めて失敗にする。
    throw new Error(`run ended without terminal event${acc ? ` (partial: ${acc.length} chars)` : ''}`)
  }

  return { start, events, answer }
}

/** RunEvent エンベロープ（run_id/type/seq/ts）を持つか。keepalive など非 RunEvent を弾く。 */
function isRunEvent(v: unknown): v is RunEvent {
  return (
    typeof v === 'object' &&
    v !== null &&
    'run_id' in v &&
    'type' in v &&
    'seq' in v &&
    'ts' in v
  )
}

/** HTTP エラーを detail 付きで拾う（生 URL は含めない）。 */
async function httpError(res: Response): Promise<Error> {
  const detail = await res
    .json()
    .then((d: { detail?: string }) => d?.detail)
    .catch(() => null)
  return new Error(detail ?? `HTTP ${res.status}`)
}

/** readSse（callback 式）を AsyncIterable に橋渡しする。
 *
 *  readSse は「SSE を読む」責務の単一実装なので再利用し、push→pull の変換だけをここで足す。
 *  単一スレッドの JS では、キュー drain 中は callback が走らず（await 点なし）、待機の
 *  Promise を張ってから制御を手放すので、通知の取りこぼしは起きない。
 */
async function* sseToAsyncIterable<T>(res: Response, signal?: AbortSignal): AsyncGenerator<T> {
  const queue: T[] = []
  let notify: (() => void) | null = null
  let done = false
  let failure: unknown = null

  const wait = () =>
    new Promise<void>((resolve) => {
      notify = resolve
    })
  const wake = () => {
    const n = notify
    notify = null
    n?.()
  }

  // 早期 break で generator が閉じられたら readSse を abort 経由で確実に終わらせる（reader cancel）。
  // AbortError は握りつぶす（消費側の意図的な中断であって障害ではない）。
  readSse<T>(res, (ev) => {
    queue.push(ev)
    wake()
  }, { signal, silentAbort: true }).then(
    () => {
      done = true
      wake()
    },
    (err) => {
      failure = err
      done = true
      wake()
    },
  )

  for (;;) {
    while (queue.length) yield queue.shift() as T
    if (done) {
      if (failure) throw failure
      return
    }
    await wait()
  }
}
