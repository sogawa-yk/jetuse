import { describe, it, expect } from 'vitest'
import { buildChatRequest, type BuildChatRequestParams } from './buildChatRequest'
import type { Msg } from './types'

const history: Msg[] = [
  { role: 'user', content: 'hello' },
  { role: 'assistant', content: 'hi' },
]

/** 既定値で埋めたパラメータ。各テストで必要な所だけ上書きする。 */
function params(over: Partial<BuildChatRequestParams> = {}): BuildChatRequestParams {
  return {
    model: 'gpt-oss-120b',
    history,
    systemPrompt: '',
    temperature: 0.7,
    topP: 1,
    maxTokens: '',
    effort: '',
    isReasoning: false,
    conversationId: 'c1',
    persistUser: true,
    agentDefId: null,
    selectedTools: [],
    selectedMcp: [],
    autoTools: false,
    toolResults: null,
    sendImages: null,
    sdkResume: null,
    ...over,
  }
}

describe('buildChatRequest', () => {
  it('maps model and strips messages to role/content only', () => {
    const body = buildChatRequest(params())
    expect(body.model).toBe('gpt-oss-120b')
    expect(body.messages).toEqual([
      { role: 'user', content: 'hello' },
      { role: 'assistant', content: 'hi' },
    ])
  })

  it('prepends a system message only when systemPrompt is non-empty (trimmed)', () => {
    expect(buildChatRequest(params({ systemPrompt: '  ' })).messages[0]).toEqual({
      role: 'user',
      content: 'hello',
    })
    const withSys = buildChatRequest(params({ systemPrompt: '  be terse  ' }))
    expect(withSys.messages[0]).toEqual({ role: 'system', content: 'be terse' })
    expect(withSys.messages).toHaveLength(3)
  })

  it('sends top_p only when below 1 (else null = model default)', () => {
    expect(buildChatRequest(params({ topP: 1 })).top_p).toBeNull()
    expect(buildChatRequest(params({ topP: 0.9 })).top_p).toBe(0.9)
  })

  it('parses max_tokens from string, null when blank', () => {
    expect(buildChatRequest(params({ maxTokens: '' })).max_tokens).toBeNull()
    expect(buildChatRequest(params({ maxTokens: '  ' })).max_tokens).toBeNull()
    expect(buildChatRequest(params({ maxTokens: '2048' })).max_tokens).toBe(2048)
  })

  it('sends reasoning_effort only for reasoning models with a chosen effort', () => {
    expect(buildChatRequest(params({ isReasoning: false, effort: 'high' })).reasoning_effort).toBeNull()
    expect(buildChatRequest(params({ isReasoning: true, effort: '' })).reasoning_effort).toBeNull()
    expect(buildChatRequest(params({ isReasoning: true, effort: 'high' })).reasoning_effort).toBe('high')
  })

  it('enables ad-hoc agent mode only when no saved agent + tools/mcp selected + reasoning', () => {
    // reasoning + tools, no saved agent → agent:true
    expect(
      buildChatRequest(params({ isReasoning: true, selectedTools: ['web_search'] })).agent,
    ).toBe(true)
    // mcp selected counts too
    expect(buildChatRequest(params({ isReasoning: true, selectedMcp: ['m1'] })).agent).toBe(true)
    // non-reasoning model → false
    expect(
      buildChatRequest(params({ isReasoning: false, selectedTools: ['web_search'] })).agent,
    ).toBe(false)
    // no tools/mcp → false
    expect(buildChatRequest(params({ isReasoning: true })).agent).toBe(false)
    // saved agent present → ad-hoc agent mode disabled
    expect(
      buildChatRequest(
        params({ isReasoning: true, selectedTools: ['web_search'], agentDefId: 'a1' }),
      ).agent,
    ).toBe(false)
  })

  it('passes agent_id through', () => {
    expect(buildChatRequest(params({ agentDefId: 'a1' })).agent_id).toBe('a1')
    expect(buildChatRequest(params()).agent_id).toBeNull()
  })

  it('nulls enabled_tools / mcp_server_ids when empty, lists when present', () => {
    const empty = buildChatRequest(params())
    expect(empty.enabled_tools).toBeNull()
    expect(empty.mcp_server_ids).toBeNull()
    const filled = buildChatRequest(params({ selectedTools: ['a'], selectedMcp: ['m'] }))
    expect(filled.enabled_tools).toEqual(['a'])
    expect(filled.mcp_server_ids).toEqual(['m'])
  })

  it('nulls images when none, passes array when present', () => {
    expect(buildChatRequest(params({ sendImages: null })).images).toBeNull()
    expect(buildChatRequest(params({ sendImages: [] })).images).toBeNull()
    expect(buildChatRequest(params({ sendImages: ['data:img'] })).images).toEqual(['data:img'])
  })

  it('passes tool_results and persist_user through', () => {
    const tr = [{ call: { id: 'x' }, output: 'ok' }]
    const body = buildChatRequest(params({ toolResults: tr, persistUser: false }))
    expect(body.tool_results).toBe(tr)
    expect(body.persist_user).toBe(false)
  })

  it('maps sdkResume to sdk_state / sdk_approvals (null when absent)', () => {
    const none = buildChatRequest(params())
    expect(none.sdk_state).toBeNull()
    expect(none.sdk_approvals).toBeNull()
    const resume = buildChatRequest(
      params({ sdkResume: { state: 'STATE', approvals: { call1: true } } }),
    )
    expect(resume.sdk_state).toBe('STATE')
    expect(resume.sdk_approvals).toEqual({ call1: true })
  })
})
