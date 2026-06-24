/** カスタムエージェント一覧(feedback 2026-06-15): 作成済みエージェントを一覧で確認・編集・実行する */
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { authHeaders, useUser } from '../auth'
import { PageContainer } from '../components/layout'
import { DataTable, StatusBadge, type Column } from '../components/oci'
import { usePrefs } from '../prefs'

type AgentRow = {
  id: string
  name: string
  description?: string | null
  icon?: string | null
  framework?: string | null
  visibility?: string
  mine?: boolean
}

const ENGINE_KEY = {
  openai_agents: 'agent.framework.openai',
  adk: 'agent.framework.adk',
  langgraph: 'agent.framework.langgraph',
  select_ai: 'agent.framework.select_ai',
  // 旧framework値の後方互換
  agents_sdk: 'agent.framework.openai',
  native: 'agent.framework.openai',
  hosted: 'agent.framework.openai',
} as const

export default function Agents() {
  const { t } = usePrefs()
  const user = useUser()
  const [agents, setAgents] = useState<AgentRow[]>([])

  useEffect(() => {
    fetch('/api/agents', { headers: authHeaders(user) })
      .then((r) => r.json())
      .then((d) => setAgents(d.agents ?? []))
      .catch(() => setAgents([]))
  }, [user])

  const columns: Column<AgentRow>[] = [
    {
      key: 'name',
      label: t('agents.col.name'),
      render: (a) => (
        <Link to={`/chat?agent=${a.id}`} className="flex items-center gap-2 text-action hover:underline">
          <span aria-hidden>{a.icon || '🤖'}</span>
          <span className="font-medium">{a.name}</span>
          {a.mine && (
            <span className="rounded-full bg-band-chip/20 px-1.5 text-[10px] text-ink-muted">
              {t('agents.mine')}
            </span>
          )}
        </Link>
      ),
    },
    {
      key: 'engine',
      label: t('agents.col.engine'),
      render: (a) => (
        <span className="text-xs text-ink-muted">
          {t(
            ENGINE_KEY[(a.framework as keyof typeof ENGINE_KEY) ?? 'openai_agents'] ??
              'agent.framework.openai',
          ).replace(/^(SDK|実装|Engine): /, '')}
        </span>
      ),
    },
    {
      key: 'visibility',
      label: t('agents.col.visibility'),
      render: (a) =>
        a.visibility === 'public' ? (
          <StatusBadge kind="ok">{t('home.shared')}</StatusBadge>
        ) : (
          <StatusBadge kind="neutral">{t('uc.builder.private')}</StatusBadge>
        ),
    },
    {
      key: 'actions',
      label: t('agents.col.actions'),
      className: 'text-right',
      render: (a) => (
        <span className="flex justify-end gap-3 text-xs">
          <Link to={`/chat?agent=${a.id}`} className="text-action hover:underline">
            ▶ {t('agents.open')}
          </Link>
          {a.mine && (
            <Link to={`/agents/${a.id}`} className="text-ink-muted hover:text-action hover:underline">
              ✎ {t('uc.edit')}
            </Link>
          )}
        </span>
      ),
    },
  ]

  return (
    <PageContainer
      icon="agents"
      title={t('agents.title')}
      subtitle={t('agents.lead')}
      helpKey="agents"
      action={
        <Link
          to="/agents/new"
          className="rounded-rw bg-cta px-3 py-1.5 text-sm font-medium text-cta-ink hover:bg-cta-strong"
        >
          ＋ {t('agent.new')}
        </Link>
      }
    >
      {agents.length === 0 ? (
        <p className="rounded-rw border border-dashed border-line bg-surface/60 px-4 py-10 text-center text-sm text-ink-muted">
          {t('agents.empty')}
        </p>
      ) : (
        <div className="rounded-rw-lg bg-surface p-2 shadow-rw">
          <DataTable columns={columns} rows={agents} rowKey={(a) => a.id} />
        </div>
      )}
    </PageContainer>
  )
}
