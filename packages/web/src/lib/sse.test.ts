import { describe, it, expect, vi } from 'vitest'
import { readSse, SseUnauthorizedError } from './sse'

/** 文字列チャンク群を、指定のバイト境界で区切って流す ReadableStream を作る。
 *  bytewise=true のとき1バイトずつ流し、TextDecoder のバッファリング（マルチバイト
 *  文字や "\n\n" がチャンクをまたぐケース）を検証できるようにする。 */
function streamFrom(chunks: string[], bytewise = false): ReadableStream<Uint8Array> {
  const enc = new TextEncoder()
  let bytes: Uint8Array[]
  if (bytewise) {
    const all = enc.encode(chunks.join(''))
    bytes = Array.from(all, (b) => Uint8Array.of(b))
  } else {
    bytes = chunks.map((c) => enc.encode(c))
  }
  let i = 0
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i < bytes.length) {
        controller.enqueue(bytes[i++])
      } else {
        controller.close()
      }
    },
  })
}

/** テスト用の最小 Response。status と SSE body を指定。 */
function sseResponse(chunks: string[], opts?: { status?: number; bytewise?: boolean; noBody?: boolean }): Response {
  const status = opts?.status ?? 200
  if (opts?.noBody) {
    // 401など、本文を読まないケース用に body=null の Response を作る。
    return new Response(null, { status })
  }
  return new Response(streamFrom(chunks, opts?.bytewise), { status })
}

const data = (obj: unknown) => `data: ${JSON.stringify(obj)}\n\n`

describe('readSse', () => {
  it('parses a single data event', async () => {
    const res = sseResponse([data({ delta: 'hello' })])
    const events: { delta?: string }[] = []
    await readSse<{ delta?: string }>(res, (ev) => events.push(ev))
    expect(events).toEqual([{ delta: 'hello' }])
  })

  it('parses multiple events in a single chunk', async () => {
    const res = sseResponse([data({ delta: 'a' }) + data({ delta: 'b' }) + data({ delta: 'c' })])
    const events: { delta?: string }[] = []
    await readSse<{ delta?: string }>(res, (ev) => events.push(ev))
    expect(events.map((e) => e.delta)).toEqual(['a', 'b', 'c'])
  })

  it('buffers events split across arbitrary chunk boundaries', async () => {
    const raw = data({ delta: 'one' }) + data({ delta: 'two' })
    // "data:" やJSON、"\n\n" 区切りがチャンクをまたぐように分割。
    const chunks = [raw.slice(0, 5), raw.slice(5, 11), raw.slice(11, 22), raw.slice(22)]
    const res = sseResponse(chunks)
    const events: { delta?: string }[] = []
    await readSse<{ delta?: string }>(res, (ev) => events.push(ev))
    expect(events.map((e) => e.delta)).toEqual(['one', 'two'])
  })

  it('reassembles a stream delivered one byte at a time (incl. multibyte JP)', async () => {
    const raw = data({ text: 'こんにちは' }) + data({ text: '世界' })
    const res = sseResponse([raw], { bytewise: true })
    const events: { text?: string }[] = []
    await readSse<{ text?: string }>(res, (ev) => events.push(ev))
    expect(events.map((e) => e.text)).toEqual(['こんにちは', '世界'])
  })

  it('skips the [DONE] sentinel', async () => {
    const res = sseResponse([data({ delta: 'x' }) + 'data: [DONE]\n\n'])
    const events: unknown[] = []
    await readSse(res, (ev) => events.push(ev))
    expect(events).toEqual([{ delta: 'x' }])
  })

  it('skips non-data: lines (comments / blank keepalives)', async () => {
    const res = sseResponse([': keepalive\n\n' + 'event: ping\n\n' + data({ delta: 'y' })])
    const events: { delta?: string }[] = []
    await readSse<{ delta?: string }>(res, (ev) => events.push(ev))
    expect(events).toEqual([{ delta: 'y' }])
  })

  it('supports generic typed events (multiple shapes)', async () => {
    type Ev = { delta?: string; tool_call?: { name: string }; error?: string }
    const res = sseResponse([
      data({ delta: 'hi' }) + data({ tool_call: { name: 'search' } }) + data({ error: 'boom' }),
    ])
    const events: Ev[] = []
    await readSse<Ev>(res, (ev) => events.push(ev))
    expect(events).toEqual([{ delta: 'hi' }, { tool_call: { name: 'search' } }, { error: 'boom' }])
  })

  describe('parse errors', () => {
    it('throws on malformed JSON by default', async () => {
      const res = sseResponse(['data: {not json}\n\n'])
      await expect(readSse(res, () => {})).rejects.toBeInstanceOf(SyntaxError)
    })

    it('skips malformed JSON when onParseError is "ignore"', async () => {
      const res = sseResponse(['data: {bad}\n\n' + data({ delta: 'ok' })])
      const events: { delta?: string }[] = []
      await readSse<{ delta?: string }>(res, (ev) => events.push(ev), { onParseError: 'ignore' })
      expect(events).toEqual([{ delta: 'ok' }])
    })

    it('calls onParseError callback and continues', async () => {
      const res = sseResponse(['data: {bad}\n\n' + data({ delta: 'ok' })])
      const onParseError = vi.fn()
      const events: { delta?: string }[] = []
      await readSse<{ delta?: string }>(res, (ev) => events.push(ev), { onParseError })
      expect(onParseError).toHaveBeenCalledTimes(1)
      expect(onParseError.mock.calls[0][1]).toBe('{bad}')
      expect(events).toEqual([{ delta: 'ok' }])
    })
  })

  describe('401 handling', () => {
    it('on401="throw" (default): calls onUnauthorized then throws SseUnauthorizedError', async () => {
      const res = sseResponse([], { status: 401, noBody: true })
      const onUnauthorized = vi.fn()
      const onEvent = vi.fn()
      await expect(readSse(res, onEvent, { onUnauthorized })).rejects.toBeInstanceOf(
        SseUnauthorizedError,
      )
      expect(onUnauthorized).toHaveBeenCalledTimes(1)
      expect(onEvent).not.toHaveBeenCalled()
    })

    it('on401="reauth-return": calls onUnauthorized and returns without reading', async () => {
      const res = sseResponse([], { status: 401, noBody: true })
      const onUnauthorized = vi.fn()
      const onEvent = vi.fn()
      await expect(
        readSse(res, onEvent, { on401: 'reauth-return', onUnauthorized }),
      ).resolves.toBeUndefined()
      expect(onUnauthorized).toHaveBeenCalledTimes(1)
      expect(onEvent).not.toHaveBeenCalled()
    })

    it('does not read the body on 401 even if one is present', async () => {
      const res = sseResponse([data({ delta: 'leak' })], { status: 401 })
      const onEvent = vi.fn()
      await readSse(res, onEvent, { on401: 'reauth-return' })
      expect(onEvent).not.toHaveBeenCalled()
    })
  })

  describe('abort handling', () => {
    it('rethrows AbortError by default', async () => {
      const ac = new AbortController()
      // signal連動の本物のabortを再現するため、pull中にabortして読み取りを失敗させる。
      const body = new ReadableStream<Uint8Array>({
        pull(controller) {
          if (ac.signal.aborted) {
            controller.error(new DOMException('aborted', 'AbortError'))
            return
          }
          ac.abort()
          controller.enqueue(new TextEncoder().encode('data: {}\n\n'))
        },
      })
      const res = new Response(body, { status: 200 })
      await expect(
        readSse(res, () => {}, { signal: ac.signal }),
      ).rejects.toMatchObject({ name: 'AbortError' })
    })

    it('swallows AbortError when silentAbort is true', async () => {
      const ac = new AbortController()
      const body = new ReadableStream<Uint8Array>({
        pull(controller) {
          controller.error(new DOMException('aborted', 'AbortError'))
        },
      })
      ac.abort()
      const res = new Response(body, { status: 200 })
      await expect(
        readSse(res, () => {}, { signal: ac.signal, silentAbort: true }),
      ).resolves.toBeUndefined()
    })

    it('still rethrows non-abort errors even with silentAbort', async () => {
      const body = new ReadableStream<Uint8Array>({
        pull(controller) {
          controller.error(new Error('network blip'))
        },
      })
      const res = new Response(body, { status: 200 })
      await expect(
        readSse(res, () => {}, { silentAbort: true }),
      ).rejects.toThrow('network blip')
    })
  })

  it('throws if response has no body and status is not 401', async () => {
    const res = new Response(null, { status: 200 })
    await expect(readSse(res, () => {})).rejects.toThrow(/no body/)
  })

  it('completes normally on an empty stream', async () => {
    const res = sseResponse([])
    const onEvent = vi.fn()
    await expect(readSse(res, onEvent)).resolves.toBeUndefined()
    expect(onEvent).not.toHaveBeenCalled()
  })
})
