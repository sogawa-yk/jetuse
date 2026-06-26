/** デモ構成プレビュー(HBD-03)。
 *
 *  ヒアリング推薦から合成したデモ構成(POST /api/hearing/sessions/:sid/preview)を、
 *  **実行せずに**描画する。画面・組込点(どこにどのAIが入るか)・使うAI(束縛状態)・
 *  使うデータ(シード方針)を宣言定義のままレンダリングし、デプロイ前にSAが構成を確認できる。
 *
 *  合成・束縛・整合チェックはすべて API 側(jetuse_core.synth)が担う。本ページは描画専用で、
 *  AIスロットを実行しない(プレビューは再検証可能な配布表現を壊さない)。 */
import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { authHeaders, reauthenticate, useUser } from '../auth'
import { PageContainer } from '../components/layout'
import { DataTable, Panel, StatusBadge, type Column } from '../components/oci'

export type SlotBinding = {
  capability: string
  status: 'active' | 'unbound' | 'no_slot'
  slot_keys: string[]
  screen_keys: string[]
  title: string | null
  highlight: boolean
  permissions: string[]
  reason: string | null
}

export type ScreenView = {
  key: string
  title: string
  type: string
  dataset: string | null
  slots: { slot_key: string; capability: string; title: string; highlight: boolean }[]
}

export type SeedDataset = { name: string; label: string; fields: number; seed_rows: number }
export type SeedPlan = {
  strategy: string
  note: string
  seeded: boolean
  datasets: SeedDataset[]
  total_seed_rows: number
}

export type CompositionReport = {
  ok: boolean
  required_capabilities: string[]
  required_permissions: string[]
  missing_capabilities: string[]
  undeclared_permissions: string[]
  unused_permissions: string[]
}

export type DemoComposition = {
  ok: boolean
  sample_app: string | null
  instance_id: string | null
  app_name: string | null
  summary: string
  icon: string
  ui: string | null
  connectors: string[]
  highlight: string | null
  screens: ScreenView[]
  bindings: SlotBinding[]
  active_parts: string[]
  excluded: { capability: string; status: string; reason: string }[]
  seed: SeedPlan
  composition_report: CompositionReport | null
  warnings: string[]
  errors: string[]
}

const STATUS_BADGE: Record<SlotBinding['status'], { kind: 'ok' | 'warn' | 'neutral'; label: string }> = {
  active: { kind: 'ok', label: '実行可能' },
  unbound: { kind: 'warn', label: '未束縛' },
  no_slot: { kind: 'neutral', label: '組込点なし' },
}

const SEED_LABEL: Record<string, string> = {
  sample: 'サンプルシード',
  genai_generated: 'GenAI 業界寄せ生成',
  replace_later: '後で実データ差替',
}

const UI_LABEL: Record<string, string> = {
  chat: 'チャットUI',
  notify: '通知',
  report: 'レポート',
}

/** デモ構成オブジェクトを描画する純粋コンポーネント(fetch を含まない=テスト容易)。 */
export function CompositionPreview({ comp }: { comp: DemoComposition }) {
  if (!comp.ok) {
    return (
      <Panel title="合成できませんでした" className="border border-pill-err">
        <p className="mb-2 text-sm text-ink-muted">
          推薦から実行可能なデモ構成を合成できませんでした。推薦を見直してください。
        </p>
        <ul className="list-disc pl-5 text-sm text-pill-err-ink" data-testid="errors">
          {comp.errors.map((e, i) => (
            <li key={i}>{e}</li>
          ))}
        </ul>
      </Panel>
    )
  }

  const bindingCols: Column<SlotBinding>[] = [
    { key: 'capability', label: 'AI部品', render: (b) => <code>{b.capability}</code> },
    { key: 'title', label: '組込点', render: (b) => b.title ?? '—' },
    {
      key: 'status',
      label: '状態',
      render: (b) => {
        const s = STATUS_BADGE[b.status]
        return (
          <span className="inline-flex items-center gap-2">
            <StatusBadge kind={s.kind}>{s.label}</StatusBadge>
            {b.highlight && <StatusBadge kind="ok">主役</StatusBadge>}
          </span>
        )
      },
    },
    {
      key: 'screens',
      label: '画面',
      render: (b) => (b.screen_keys.length ? b.screen_keys.join(', ') : '—'),
    },
  ]

  const seedCols: Column<SeedDataset>[] = [
    { key: 'label', label: 'データセット', render: (d) => d.label },
    { key: 'fields', label: '列数', render: (d) => String(d.fields) },
    { key: 'seed_rows', label: 'シード行数', render: (d) => String(d.seed_rows) },
  ]

  return (
    <div className="flex flex-col gap-4">
      <Panel
        title={
          <span className="flex items-center gap-2">
            <span aria-hidden>{comp.icon}</span>
            <span data-testid="app-name">{comp.app_name ?? comp.sample_app}</span>
          </span>
        }
        action={
          <span className="flex items-center gap-2">
            <StatusBadge kind="neutral">{UI_LABEL[comp.ui ?? ''] ?? comp.ui ?? 'UI未指定'}</StatusBadge>
            {comp.connectors.map((c) => (
              <StatusBadge key={c} kind="neutral">
                🔌 {c}
              </StatusBadge>
            ))}
          </span>
        }
      >
        <p className="text-sm text-ink-muted">{comp.summary}</p>
      </Panel>

      {comp.warnings.length > 0 && (
        <Panel title="注意" className="border border-pill-warn">
          <ul className="list-disc pl-5 text-sm text-pill-warn-ink" data-testid="warnings">
            {comp.warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </Panel>
      )}

      <Panel title="画面と組込点">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3" data-testid="screens">
          {comp.screens.map((s) => (
            <div key={s.key} className="rounded-rw bg-bg p-3" data-testid={`screen-${s.key}`}>
              <div className="flex items-center justify-between">
                <span className="font-semibold">{s.title}</span>
                <StatusBadge kind="neutral">{s.type}</StatusBadge>
              </div>
              {s.dataset && (
                <div className="mt-1 text-xs text-ink-muted">データ: {s.dataset}</div>
              )}
              <div className="mt-2 flex flex-wrap gap-1">
                {s.slots.length === 0 && (
                  <span className="text-xs text-ink-muted">組込点なし</span>
                )}
                {s.slots.map((sl) => (
                  <StatusBadge key={sl.slot_key} kind={sl.highlight ? 'ok' : 'neutral'}>
                    🤖 {sl.title}
                  </StatusBadge>
                ))}
              </div>
            </div>
          ))}
        </div>
      </Panel>

      <Panel title="使う AI(部品の束縛)">
        <DataTable
          columns={bindingCols}
          rows={comp.bindings}
          rowKey={(b) => b.capability}
        />
      </Panel>

      <Panel
        title="使うデータ(シード)"
        action={<StatusBadge kind="neutral">{SEED_LABEL[comp.seed.strategy] ?? comp.seed.strategy}</StatusBadge>}
      >
        <p className="mb-2 text-sm text-ink-muted" data-testid="seed-note">
          {comp.seed.note}
        </p>
        <DataTable
          columns={seedCols}
          rows={comp.seed.datasets}
          rowKey={(d) => d.name}
        />
        <div className="mt-2 text-xs text-ink-muted">
          投入予定シード総数: {comp.seed.total_seed_rows} 行
        </div>
      </Panel>
    </div>
  )
}

export default function Preview() {
  const { sid } = useParams()
  const user = useUser()
  const [comp, setComp] = useState<DemoComposition | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (!sid) return
    let cancelled = false
    fetch(`/api/hearing/sessions/${sid}/preview`, {
      method: 'POST',
      headers: authHeaders(user),
    })
      .then(async (r) => {
        if (r.status === 401) {
          reauthenticate()
          throw new Error('unauthorized')
        }
        if (!r.ok) {
          const body = await r.json().catch(() => ({}))
          throw new Error(body.detail || `エラー (${r.status})`)
        }
        return r.json() as Promise<DemoComposition>
      })
      .then((c) => {
        if (!cancelled) setComp(c)
      })
      .catch((e) => {
        if (!cancelled) setError(String(e.message ?? e))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [sid, user])

  return (
    <PageContainer
      icon="diagram"
      title="デモ構成プレビュー"
      subtitle="推薦から合成したデモ構成を、デプロイ前に確認します"
    >
      <div className="mb-3 text-sm">
        <Link to="/builder" className="text-link hover:underline">
          ← ビルダーへ戻る
        </Link>
      </div>
      {loading && <p className="text-sm text-ink-muted">合成中…</p>}
      {error && (
        <Panel title="読み込みエラー" className="border border-pill-err">
          <p className="text-sm text-pill-err-ink">{error}</p>
        </Panel>
      )}
      {comp && <CompositionPreview comp={comp} />}
    </PageContainer>
  )
}
