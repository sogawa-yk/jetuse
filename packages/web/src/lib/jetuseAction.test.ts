import { describe, it, expect, vi } from 'vitest'
import { createAnswerClient, type RunEvent } from './jetuseAction'

/** 完全な RunEvent（SSE が配信する形）を data: フレーム列にする。 */
function sseBody(events: RunEvent[]): string {
  return events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join('') + 'data: [DONE]\n\n'
}
function sseResponse(events: RunEvent[]): Response {
  const enc = new TextEncoder()
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      c.enqueue(enc.encode(sseBody(events)))
      c.close()
    },
  })
  return new Response(stream, { status: 200 })
}

const ev = (e: Partial<RunEvent> & { type: RunEvent['type'] }): RunEvent =>
  ({ run_id: 'r1', seq: 0, ts: '2026-07-01T00:00:00Z', data: {}, ...e }) as RunEvent

const RUN_EVENTS: RunEvent[] = [
  ev({ type: 'run.started' }),
  ev({ type: 'retrieval.started' }),
  ev({ type: 'retrieval.completed', data: { citations: [{ source: 'doc.pdf#p1', score: 0.9 }] } }),
  ev({ type: 'message.delta', data: { text: 'こんにちは' } }),
  ev({ type: 'message.delta', data: { text: '、世界' } }),
  ev({
    type: 'run.completed',
    data: { output: { answer: 'こんにちは、世界', citations: [{ source: 'doc.pdf#p1' }] } },
  }),
]

/** start(POST→run_id) と events(GET→SSE) を経路で振り分ける mock fetch。 */
function mockFetch(events: RunEvent[] = RUN_EVENTS) {
  const calls: { url: string; init?: RequestInit }[] = []
  const fetchImpl = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const u = String(url)
    calls.push({ url: u, init })
    if (init?.method === 'POST') {
      return new Response(JSON.stringify({ run_id: 'r1', status: 'queued' }), { status: 202 })
    }
    return sseResponse(events)
  }) as unknown as typeof fetch
  return { fetchImpl, calls }
}

describe('createAnswerClient', () => {
  it('events() が RunEvent を順に yield し、delta 結合と run.completed の output を取り出せる', async () => {
    const { fetchImpl } = mockFetch()
    const client = createAnswerClient({ experienceId: 'exp1', fetchImpl })

    const types: string[] = []
    let acc = ''
    let output: unknown = null
    for await (const e of client.events('r1')) {
      types.push(e.type)
      if (e.type === 'message.delta') acc += e.data.text
      if (e.type === 'run.completed') output = e.data.output
    }
    expect(types).toEqual([
      'run.started',
      'retrieval.started',
      'retrieval.completed',
      'message.delta',
      'message.delta',
      'run.completed',
    ])
    expect(acc).toBe('こんにちは、世界')
    expect(output).toEqual({ answer: 'こんにちは、世界', citations: [{ source: 'doc.pdf#p1' }] })
  })

  it('start() は論理 experienceId から内部でパスを組み立て run_id を返す（生 URL は呼び出し側非露出）', async () => {
    const { fetchImpl, calls } = mockFetch()
    const client = createAnswerClient({ experienceId: 'exp1', fetchImpl })
    const { runId } = await client.start({ question: 'Q' })
    expect(runId).toBe('r1')
    // 呼び出し側は URL を一切渡していないのにクライアントが正しいパスを構築している。
    expect(calls[0].url).toBe(
      '/api/v1/experiences/exp1/actions/answer.with-citations%401/runs',
    )
    expect(calls[0].init?.method).toBe('POST')
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({ question: 'Q' })
  })

  it('answer() は start+購読+集約し、onDelta を呼びつつ最終 output を返す', async () => {
    const { fetchImpl } = mockFetch()
    const client = createAnswerClient({ experienceId: 'exp1', fetchImpl })
    const deltas: string[] = []
    const out = await client.answer({ question: 'Q' }, (t) => deltas.push(t))
    expect(deltas).toEqual(['こんにちは', '、世界'])
    expect(out.answer).toBe('こんにちは、世界')
    expect(out.citations).toEqual([{ source: 'doc.pdf#p1' }])
  })

  it('注入した認証ヘッダを両リクエストに載せる', async () => {
    const { fetchImpl, calls } = mockFetch()
    const client = createAnswerClient({
      experienceId: 'exp1',
      fetchImpl,
      headers: () => ({ Authorization: 'Bearer T' }),
    })
    await client.answer({ question: 'Q' })
    for (const c of calls) {
      expect((c.init?.headers as Record<string, string>).Authorization).toBe('Bearer T')
    }
  })

  it('run.failed で answer() は throw する', async () => {
    const failEvents: RunEvent[] = [
      ev({ type: 'run.started' }),
      ev({ type: 'run.failed', data: { error: 'boom', code: 'provider_error' } }),
    ]
    const { fetchImpl } = mockFetch(failEvents)
    const client = createAnswerClient({ experienceId: 'exp1', fetchImpl })
    await expect(client.answer({ question: 'Q' })).rejects.toThrow('boom')
  })

  it('events() は keepalive フレーム（{"ka":1}）を RunEvent として yield しない', async () => {
    // Run API は待機中に data: {"ka":1} を送る。これを公開型契約から除外することを検証。
    const enc = new TextEncoder()
    const body =
      `data: ${JSON.stringify(ev({ type: 'run.started' }))}\n\n` +
      `data: {"ka":1}\n\n` +
      `data: ${JSON.stringify(ev({ type: 'run.completed', data: { output: { answer: 'a', citations: [] } } }))}\n\n` +
      'data: [DONE]\n\n'
    const fetchImpl = vi.fn(async () =>
      new Response(
        new ReadableStream<Uint8Array>({
          start(c) {
            c.enqueue(enc.encode(body))
            c.close()
          },
        }),
        { status: 200 },
      ),
    ) as unknown as typeof fetch
    const client = createAnswerClient({ experienceId: 'exp1', fetchImpl })
    const types: string[] = []
    for await (const e of client.events('r1')) types.push(e.type)
    expect(types).toEqual(['run.started', 'run.completed']) // {"ka":1} は現れない
  })

  it('events() の早期 break で購読を abort する（reader/サーバ購読の解放）', async () => {
    const captured: { signal?: AbortSignal | null } = {}
    const enc = new TextEncoder()
    const fetchImpl = vi.fn(async (_url: string, init?: RequestInit) => {
      captured.signal = init?.signal
      // 終端を送らず開き続けるストリーム（消費側が break するまで閉じない）。
      return new Response(
        new ReadableStream<Uint8Array>({
          start(c) {
            c.enqueue(enc.encode(`data: ${JSON.stringify(ev({ type: 'run.started' }))}\n\n`))
            // 意図的に close しない。
          },
        }),
        { status: 200 },
      )
    }) as unknown as typeof fetch
    const client = createAnswerClient({ experienceId: 'exp1', fetchImpl })
    for await (const e of client.events('r1')) {
      expect(e.type).toBe('run.started')
      break // 最初のイベントで離脱 → generator の finally が abort するはず。
    }
    expect(captured.signal?.aborted).toBe(true)
  })

  it('question 空は start() で早期に弾く', async () => {
    const { fetchImpl } = mockFetch()
    const client = createAnswerClient({ experienceId: 'exp1', fetchImpl })
    await expect(client.start({ question: '' })).rejects.toThrow('question is required')
  })
})
