/** Agent Builder(AGT-03): エージェントのCRUD。Project割当で記憶分離 */
import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { authHeaders, useUser } from '../auth'
import { IconPicker } from '../components/icons'
import { PageContainer } from '../components/layout'
import { usePrefs } from '../prefs'

const inputCls =
  'rounded-rw border border-line bg-surface px-2 py-1.5 text-sm outline-none focus:border-action'

export default function AgentBuilder() {
  const { id } = useParams()
  const { t } = usePrefs()
  const user = useUser()
  const nav = useNavigate()
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [icon, setIcon] = useState('agents')
  const [instructions, setInstructions] = useState('')
  const [model, setModel] = useState('gpt-oss-120b')
  const [models, setModels] = useState<{ key: string; label: string; api?: string }[]>([])
  const [tools, setTools] = useState<{ name: string; label: string }[]>([])
  // ENH-04: Select AI Agent のツール一覧(SQL/RAG)
  const [saiTools, setSaiTools] = useState<{ name: string; label: string; description: string }[]>([])
  const [enabledTools, setEnabledTools] = useState<string[]>([])
  const [visibility, setVisibility] = useState<'private' | 'public'>('private')
  // AGT-MULTI(ADR-0009): SDK選択=ホスト型ReActコンテナのrouting先
  const [framework, setFramework] =
    useState<'openai_agents' | 'adk' | 'langgraph' | 'select_ai'>('openai_agents')
  const [tags, setTags] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    const h = { headers: authHeaders(user) }
    fetch('/api/chat/models', h).then((r) => r.json()).then((d) => setModels(d.models)).catch(() => {})
    fetch('/api/agent/tools', h).then((r) => r.json()).then((d) => setTools(d.tools ?? [])).catch(() => {})
    fetch('/api/agent/select-ai-tools', h).then((r) => r.json()).then((d) => setSaiTools(d.tools ?? [])).catch(() => {})
    if (id) {
      fetch(`/api/agents/${id}`, h)
        .then((r) => r.json())
        .then((a) => {
          setName(a.name)
          setDescription(a.description ?? '')
          setIcon(a.icon ?? '🤖')
          setInstructions(a.instructions ?? '')
          setModel(a.model)
          setEnabledTools(a.enabled_tools ?? [])
          setVisibility(a.visibility ?? 'private')
          setFramework(
            (({
              agents_sdk: 'openai_agents', native: 'openai_agents', hosted: 'openai_agents',
              openai_agents: 'openai_agents', adk: 'adk', langgraph: 'langgraph',
              select_ai: 'select_ai',
            } as Record<string, 'openai_agents' | 'adk' | 'langgraph' | 'select_ai'>)[
              a.framework
            ]) ?? 'openai_agents',
          )
          setTags((a.tags ?? []).join(','))
        })
        .catch(() => setError(t('agent.notFound')))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id])

  const save = async () => {
    setSaving(true)
    setError(null)
    try {
      const res = await fetch(id ? `/api/agents/${id}` : '/api/agents', {
        method: id ? 'PUT' : 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
        body: JSON.stringify({
          name: name.trim(),
          description: description.trim() || null,
          icon: icon.trim() || null,
          instructions,
          model,
          // select_ai は sql/rag、hostedはコンテナ内蔵ツールのみ
          enabled_tools: enabledTools.filter((n) =>
            (framework === 'select_ai'
              ? ['sql', 'rag']
              : ['web_search', 'web_fetch', 'get_current_time', 'rag_search', 'query_database']
            ).includes(n),
          ),
          mcp_server_ids: [],
          auto_tools: true,
          framework,
          // 記憶はユーザー×エージェント単位で既定分離するため Project 選択UIは廃止(feedback 20260620 #1)
          project_ocid: null,
          visibility,
          tags: tags.split(',').map((s) => s.trim()).filter(Boolean),
        }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`)
      nav(`/chat?agent=${data.id ?? id}`)
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e))
    } finally {
      setSaving(false)
    }
  }

  const remove = async () => {
    if (!id || !window.confirm(t('agent.deleteConfirm'))) return
    await fetch(`/api/agents/${id}`, { method: 'DELETE', headers: authHeaders(user) })
    nav('/')
  }

  return (
    <PageContainer
      icon="agents"
      title={id ? t('agent.edit') : t('agent.new')}
      subtitle={t('agent.lead')}
      action={
        id ? (
          <button
            onClick={() => void remove()}
            className="rounded-rw border border-primary px-3 py-1.5 text-sm text-primary hover:bg-primary-strong hover:text-white"
          >
            {t('chat.preset.delete')}
          </button>
        ) : undefined
      }
    >
      <div className="mx-auto max-w-2xl space-y-4">
        <section className="rounded-rw border border-line bg-surface p-4">
          <div className="mb-3">
            <span className="mb-1.5 block text-xs font-medium text-ink-muted">
              {t('uc.builder.icon')}
            </span>
            <IconPicker value={icon} onChange={setIcon} />
          </div>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder={t('agent.name')} className={`${inputCls} w-full`} />
          <input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder={t('agent.desc')}
            className={`${inputCls} mt-2 w-full`}
          />
          <div className="mt-2">
            <span className="mb-1 block text-xs font-medium text-ink-muted">
              {t('agent.instructions')}
            </span>
            <textarea
              rows={6}
              value={instructions}
              onChange={(e) => setInstructions(e.target.value)}
              placeholder={t('agent.instructions.ph')}
              className={`${inputCls} w-full resize-y`}
            />
          </div>
        </section>

        <section className="rounded-rw border border-line bg-surface p-4">
          <div className="flex flex-wrap gap-2">
            <select value={model} onChange={(e) => setModel(e.target.value)} className={inputCls} aria-label="model">
              {models.map((m) => (
                <option key={m.key} value={m.key}>{m.label}</option>
              ))}
            </select>
            <select
              value={framework}
              onChange={(e) =>
                setFramework(e.target.value as 'openai_agents' | 'adk' | 'langgraph' | 'select_ai')
              }
              className={inputCls}
              aria-label="framework"
              title={t('agent.framework.hint')}
            >
              <option value="openai_agents">{t('agent.framework.openai')}</option>
              <option value="adk">{t('agent.framework.adk')}</option>
              <option value="langgraph">{t('agent.framework.langgraph')}</option>
              <option value="select_ai">{t('agent.framework.select_ai')}</option>
            </select>
            <select
              value={visibility}
              onChange={(e) => setVisibility(e.target.value as 'private' | 'public')}
              className={inputCls}
              aria-label="visibility"
            >
              <option value="private">{t('uc.builder.private')}</option>
              <option value="public">{t('uc.builder.public')}</option>
            </select>
            <input
              value={tags}
              onChange={(e) => setTags(e.target.value)}
              placeholder={t('uc.builder.tags')}
              className={`${inputCls} min-w-32 flex-1`}
            />
          </div>
          <p className="mt-1 text-[11px] text-ink-muted">{t('agent.framework.hint')}</p>
          <div className="mt-3 border-t border-line pt-2">
            <span className="text-xs font-medium text-ink-muted">{t('chat.tools.title')}</span>
            {framework === 'select_ai' ? (
              // ENH-04: Select AI Agent のツール(SQL/RAG)
              <div className="mt-1.5 flex flex-wrap gap-2">
                {saiTools.map((tool) => (
                  <label
                    key={tool.name}
                    title={tool.description}
                    className="flex items-center gap-1.5 rounded-rw border border-line px-2.5 py-1.5 text-xs"
                  >
                    <input
                      type="checkbox"
                      checked={enabledTools.includes(tool.name)}
                      onChange={(e) =>
                        setEnabledTools((cur) =>
                          e.target.checked
                            ? [...cur, tool.name]
                            : cur.filter((n) => n !== tool.name),
                        )
                      }
                    />
                    {tool.label}
                  </label>
                ))}
              </div>
            ) : (
              <div className="mt-1.5 flex flex-wrap gap-2">
                {tools
                  .filter((tool) =>
                    ['web_search', 'web_fetch', 'get_current_time', 'rag_search',
                      'query_database'].includes(tool.name),
                  )
                  .map((tool) => (
                    <label
                      key={tool.name}
                      className="flex items-center gap-1.5 rounded-rw border border-line px-2.5 py-1.5 text-xs"
                    >
                      <input
                        type="checkbox"
                        checked={enabledTools.includes(tool.name)}
                        onChange={(e) =>
                          setEnabledTools((cur) =>
                            e.target.checked
                              ? [...cur, tool.name]
                              : cur.filter((n) => n !== tool.name),
                          )
                        }
                      />
                      {tool.label}
                    </label>
                  ))}
              </div>
            )}
          </div>
        </section>

        <div className="flex items-center gap-3">
          <button
            onClick={() => void save()}
            disabled={saving || !name.trim() || !instructions.trim()}
            className="rounded-rw bg-cta px-4 py-2 text-sm font-medium text-cta-ink hover:bg-cta-strong disabled:opacity-40"
          >
            {t('uc.builder.save')}
          </button>
          {error && <span className="text-xs text-primary-strong">⚠ {error}</span>}
        </div>
      </div>
    </PageContainer>
  )
}
