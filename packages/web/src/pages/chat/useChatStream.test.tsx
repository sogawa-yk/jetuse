import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { useChatStream, type ChatStreamConfig } from './useChatStream'
import type { Msg } from './types'

// auth.tsx は import循環/副作用回避のためモック(reauthenticateは location 遷移を伴う)。
vi.mock('../../auth', () => ({
  authHeaders: () => ({ Authorization: 'Bearer test' }),
  reauthenticate: vi.fn(),
}))

/** data行群を1チャンクで流す SSE Response を作る。 */
function sse(events: unknown[]): Response {
  const body = events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join('') + 'data: [DONE]\n\n'
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      c.enqueue(new TextEncoder().encode(body))
      c.close()
    },
  })
  return new Response(stream, { status: 200 })
}

const baseConfig: ChatStreamConfig = {
  model: 'gpt-oss-120b',
  systemPrompt: '',
  temperature: 0.7,
  topP: 1,
  maxTokens: '',
  effort: '',
  isReasoning: true,
  agentDefId: null,
  selectedTools: ['web_search'],
  selectedMcp: [],
  autoTools: false,
}

function setup(config: Partial<ChatStreamConfig> = {}) {
  const loadConvs = vi.fn()
  const t = (k: string) => k
  return renderHook(() =>
    useChatStream({
      user: { name: 'tester' },
      t,
      loadConvs,
      getConfig: () => ({ ...baseConfig, ...config }),
    }),
  )
}

/** 指定URLへの fetch 呼び出しのうち N番目(0始まり)の JSON body を取り出す。 */
function bodyOfUrl(fetchMock: ReturnType<typeof vi.fn>, url: string, nth = 0) {
  const calls = fetchMock.mock.calls.filter((c) => c[0] === url)
  return JSON.parse((calls[nth][1] as RequestInit).body as string)
}

const STREAM = '/api/chat/stream'

const userTurn: Msg[] = [{ role: 'user', content: 'search the web' }]

describe('useChatStream', () => {
  let fetchMock: ReturnType<typeof vi.fn>
  // URLごとの応答キュー(FIFO)。/api/chat/stream のタイトルPOST等が
  // 位置依存のキューを乱さないよう、URLで振り分ける。
  let streamQueue: Response[]
  let execToolQueue: Response[]

  beforeEach(() => {
    streamQueue = []
    execToolQueue = []
    fetchMock = vi.fn((url: string) => {
      if (url === STREAM) return Promise.resolve(streamQueue.shift() ?? sse([]))
      if (url === '/api/agent/execute-tool')
        return Promise.resolve(execToolQueue.shift() ?? new Response('{}', { status: 200 }))
      // タイトル自動生成POST等は空のOK。
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    vi.stubGlobal('fetch', fetchMock)
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.clearAllMocks()
  })

  it('builds the request payload from config (model, messages, tools)', async () => {
    streamQueue.push(sse([{ delta: 'hello' }]))
    const { result } = setup()
    await act(async () => {
      await result.current.stream(userTurn, 'conv-1')
    })
    const body = bodyOfUrl(fetchMock, STREAM, 0)
    expect(body.model).toBe('gpt-oss-120b')
    expect(body.messages).toEqual([{ role: 'user', content: 'search the web' }])
    expect(body.enabled_tools).toEqual(['web_search'])
    expect(body.conversation_id).toBe('conv-1')
    expect(body.persist_user).toBe(true)
    // reasoning + tools + no saved agent → ad-hoc agent mode on
    expect(body.agent).toBe(true)
    // streamed text is appended to the assistant message
    await waitFor(() => expect(result.current.msgs.at(-1)?.content).toBe('hello'))
  })

  it('moves to pending approval when a tool_call requires approval', async () => {
    streamQueue.push(
      sse([{ tool_call: { name: 'web_search', label: 'Web検索', arguments: '{}', status: 'pending_approval' } }]),
    )
    const { result } = setup()
    await act(async () => {
      await result.current.stream(userTurn, 'conv-1')
    })
    await waitFor(() => expect(result.current.pendingCalls).not.toBeNull())
    expect(result.current.pendingCalls).toHaveLength(1)
    expect(result.current.pendingCalls?.[0].name).toBe('web_search')
  })

  it('approveTools executes the tool then resumes the stream with accumulated tool_results', async () => {
    // 1) 初回ストリーム: 承認待ちツールを返す
    streamQueue.push(
      sse([
        {
          tool_call: {
            name: 'web_search',
            label: 'Web検索',
            arguments: '{"q":"x"}',
            status: 'pending_approval',
            item: { id: 'call-1' },
          },
        },
      ]),
    )
    // 2) execute-tool: ツール実行結果
    execToolQueue.push(new Response(JSON.stringify({ output: 'RESULT' }), { status: 200 }))
    // 3) 継続ストリーム: 本文
    streamQueue.push(sse([{ delta: 'done' }]))

    const { result } = setup()
    await act(async () => {
      await result.current.stream(userTurn, 'conv-1')
    })
    await waitFor(() => expect(result.current.pendingCalls).not.toBeNull())

    await act(async () => {
      await result.current.approveTools('conv-1')
    })

    // execute-tool が name/arguments 付きで呼ばれた
    const execCall = fetchMock.mock.calls.find((c) => c[0] === '/api/agent/execute-tool')!
    expect(JSON.parse((execCall[1] as RequestInit).body as string)).toEqual({
      name: 'web_search',
      arguments: '{"q":"x"}',
    })

    // 継続ストリームは tool_results を載せ、persist_user=false で送る(2回目のstream)
    const resumeBody = bodyOfUrl(fetchMock, STREAM, 1)
    expect(resumeBody.persist_user).toBe(false)
    expect(resumeBody.tool_results).toEqual([{ call: { id: 'call-1' }, output: 'RESULT' }])
    // 承認待ちは解消
    await waitFor(() => expect(result.current.pendingCalls).toBeNull())
  })

  it('Agents SDK approval round-trip resumes with sdk_state and per-call approvals', async () => {
    // 1) SDKの承認中断(sdk_approvals + sdk_state)
    streamQueue.push(
      sse([
        {
          sdk_state: 'SDK_STATE_BLOB',
          sdk_approvals: [{ call_id: 'c1', name: 'do_thing', label: 'Do', arguments: '{}' }],
        },
      ]),
    )
    // 2) 再開ストリーム
    streamQueue.push(sse([{ delta: 'resumed' }]))

    const { result } = setup()
    await act(async () => {
      await result.current.stream(userTurn, 'conv-1')
    })
    await waitFor(() => expect(result.current.pendingCalls).not.toBeNull())
    expect(result.current.pendingCalls?.[0].kind).toBe('sdk')

    await act(async () => {
      await result.current.approveTools('conv-1')
    })

    // 再開は execute-tool を呼ばず、sdk_state と approvals を直接送る
    expect(fetchMock.mock.calls.some((c) => c[0] === '/api/agent/execute-tool')).toBe(false)
    const resumeBody = bodyOfUrl(fetchMock, STREAM, 1)
    expect(resumeBody.sdk_state).toBe('SDK_STATE_BLOB')
    expect(resumeBody.sdk_approvals).toEqual({ c1: true })
    await waitFor(() => expect(result.current.pendingCalls).toBeNull())
  })

  it('denyTools on an SDK approval resumes with approvals=false and no execute-tool call', async () => {
    streamQueue.push(
      sse([
        {
          sdk_state: 'BLOB',
          sdk_approvals: [{ call_id: 'c1', name: 'do_thing', label: 'Do', arguments: '{}' }],
        },
      ]),
    )
    streamQueue.push(sse([{ delta: 'after deny' }]))

    const { result } = setup()
    await act(async () => {
      await result.current.stream(userTurn, 'conv-1')
    })
    await waitFor(() => expect(result.current.pendingCalls).not.toBeNull())

    await act(async () => {
      result.current.denyTools('conv-1')
    })
    // 拒否でも再開ストリームが1本走る(計2本目の /api/chat/stream)
    await waitFor(() =>
      expect(fetchMock.mock.calls.filter((c) => c[0] === STREAM)).toHaveLength(2),
    )
    expect(fetchMock.mock.calls.some((c) => c[0] === '/api/agent/execute-tool')).toBe(false)
    expect(bodyOfUrl(fetchMock, STREAM, 1).sdk_approvals).toEqual({ c1: false })
  })

  it('shows the empty-response notice when the stream completes with no content', async () => {
    streamQueue.push(sse([]))
    const { result } = setup()
    await act(async () => {
      await result.current.stream(userTurn, 'conv-1')
    })
    await waitFor(() =>
      expect(result.current.msgs.at(-1)?.content).toContain('chat.emptyResp'),
    )
  })
})
