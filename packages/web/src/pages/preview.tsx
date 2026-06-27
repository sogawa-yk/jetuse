/** デモ構成プレビュー＋一気通貫の出口(HBD-03 / HBD-05)。
 *
 *  ヒアリング推薦から合成したデモ構成(POST /api/hearing/sessions/:sid/preview)を、
 *  **実行せずに**描画する。画面・組込点(どこにどのAIが入るか)・使うAI(束縛状態)・
 *  使うデータ(シード方針)を宣言定義のままレンダリングし、デプロイ前にSAが構成を確認できる。
 *
 *  さらに HBD-05 の一気通貫(プレビュー→検証→起動→サマリ)をこのページで完結させる:
 *    - 検証(POST /validate): ガバナンス4制約でデプロイ前ゲートを判定し、違反＋代替提案を提示する。
 *    - 起動(POST /launch):  検証 PASS のときだけ「このデモを起動」でき、実 loop 基盤のデモへ誘導する。
 *      検証 FAIL の構成は起動に進めず、代替提案へ誘導する(外れたデモを起動させない)。
 *    - サマリ(POST /summary): 顧客提示用の構成サマリ(構成図/使うOCIサービス/手順/効果)を生成し、
 *      Markdown でエクスポートできる(プリセールス転用)。
 *
 *  合成・束縛・整合チェック・ガバナンス判定・サマリ生成はすべて API 側が担う。本ページは
 *  描画＋導線のみで、検証/起動/サマリは利用者操作で順に進める(マウント時はプレビューのみ取得)。 */
import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { authHeaders, reauthenticate, useUser } from '../auth'
import { PageContainer } from '../components/layout'
import { DataTable, OciButton, Panel, StatusBadge, type Column } from '../components/oci'

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

// --- HBD-05: 検証(ガバナンス) / 起動 / サマリ -------------------------------

export type GovernanceViolation = {
  kind: string
  element: string
  element_type: string
  detail: string
  alternative: string
}

export type GovernanceReport = {
  ok: boolean
  sample_app: string | null
  violations: GovernanceViolation[]
  checks: Record<string, boolean>
}

export type DemoLaunch = {
  id: string
  session_id: string
  sample_app: string
  instance_id: string
  entry_slot: string | null
  demo_url: string
  status: string
  launched_at?: string | null
}

export type DiagramFlow = {
  data: string
  capability: string
  capability_label: string
  screen: string
  highlight: boolean
  line: string
}

export type OciServiceRef = { service: string; used_for: string[] }
export type DemoStep = { order: number; title: string; detail: string }

export type DemoSummary = {
  sample_app: string
  app_name: string
  ui: string | null
  connectors: string[]
  highlight: string | null
  seed_strategy: string
  diagram: DiagramFlow[]
  oci_services: OciServiceRef[]
  steps: DemoStep[]
  impact: string
  impact_source: string
  active_parts: string[]
  excluded: { capability: string; status: string; reason: string }[]
  markdown: string
}

const CHECK_LABEL: Record<string, string> = {
  allowed_combination: '許可された組合せ',
  capabilities_bound: '必要ケイパビリティ束縛',
  permission_scope: '権限スコープ',
  model_available: 'モデル可用性',
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

/** ガバナンス検証結果(デプロイ前ゲート)を描画する純粋コンポーネント。
 *  違反は機械可読(種別・該当要素・代替提案)で、外させない代替提案を必ず提示する。 */
export function ValidationPanel({ gov }: { gov: GovernanceReport }) {
  return (
    <Panel
      title="検証(デプロイ前ゲート)"
      className={gov.ok ? 'border border-pill-ok' : 'border border-pill-err'}
      action={
        <StatusBadge kind={gov.ok ? 'ok' : 'err'}>
          {gov.ok ? '✓ 検証 PASS' : '✗ 検証 FAIL'}
        </StatusBadge>
      }
    >
      <div className="mb-3 flex flex-wrap gap-2" data-testid="gov-checks">
        {Object.entries(gov.checks).map(([k, v]) => (
          <StatusBadge key={k} kind={v ? 'ok' : 'err'}>
            {v ? '✓' : '✗'} {CHECK_LABEL[k] ?? k}
          </StatusBadge>
        ))}
      </div>
      {gov.violations.length > 0 && (
        <ul className="space-y-2" data-testid="gov-violations">
          {gov.violations.map((v, i) => (
            <li key={i} className="rounded-rw border border-pill-err/40 bg-pill-err/10 px-3 py-2">
              <div className="text-sm text-pill-err-ink">
                <code>{v.element}</code> — {v.detail}
              </div>
              <div className="mt-1 text-xs text-ink-muted">
                💡 代替提案: {v.alternative}
              </div>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  )
}

/** 起動済みデモ(実 loop 基盤)の実行導線を描画する純粋コンポーネント。 */
export function LaunchPanel({ launch }: { launch: DemoLaunch }) {
  return (
    <Panel
      title="デモ起動"
      className="border border-pill-ok"
      action={<StatusBadge kind="ok">✓ 起動済み</StatusBadge>}
    >
      <p className="mb-2 text-sm text-ink-muted">
        実 loop 基盤にデモが立ち上がりました。主役 AI 機能を実際に動かして確認できます。
      </p>
      <dl className="grid grid-cols-1 gap-2 text-sm sm:grid-cols-2">
        <div>
          <dt className="text-xs text-ink-muted">サンプルアプリ</dt>
          <dd>{launch.sample_app}</dd>
        </div>
        <div>
          <dt className="text-xs text-ink-muted">実行起点スロット</dt>
          <dd>{launch.entry_slot ?? '—'}</dd>
        </div>
      </dl>
      <div className="mt-3">
        <Link
          to={launch.demo_url}
          className="text-link hover:underline"
          data-testid="run-demo-link"
        >
          → 起動したデモを開く（主役 AI 機能を実行）
        </Link>
      </div>
    </Panel>
  )
}

/** 顧客提示用の構成サマリ(構成図/使うOCIサービス/手順/効果)を描画する純粋コンポーネント。 */
export function SummaryPanel({
  summary,
  onExport,
}: {
  summary: DemoSummary
  onExport: () => void
}) {
  const svcCols: Column<OciServiceRef>[] = [
    { key: 'service', label: 'OCI サービス', render: (s) => s.service },
    { key: 'used_for', label: '用途', render: (s) => s.used_for.join(' / ') },
  ]
  return (
    <Panel
      title="構成サマリ(顧客提示用)"
      action={
        <OciButton variant="outline" onClick={onExport} data-testid="export-summary">
          Markdown をエクスポート
        </OciButton>
      }
    >
      <div className="flex flex-col gap-4">
        <section>
          <h4 className="mb-1 text-sm font-semibold">① 構成図（どのデータに何の AI が効くか）</h4>
          <ul className="list-disc pl-5 text-sm" data-testid="summary-diagram">
            {summary.diagram.map((f, i) => (
              <li key={i} className={f.highlight ? 'font-medium' : ''}>
                {f.highlight && '★ '}
                {f.line}
              </li>
            ))}
          </ul>
        </section>

        <section>
          <h4 className="mb-1 text-sm font-semibold">② 使う OCI サービス</h4>
          <DataTable columns={svcCols} rows={summary.oci_services} rowKey={(s) => s.service} />
        </section>

        <section>
          <h4 className="mb-1 text-sm font-semibold">③ デモ手順</h4>
          <ol className="list-decimal space-y-1 pl-5 text-sm" data-testid="summary-steps">
            {summary.steps.map((s) => (
              <li key={s.order}>
                <span className="font-medium">{s.title}</span>
                <span className="text-ink-muted"> — {s.detail}</span>
              </li>
            ))}
          </ol>
        </section>

        <section>
          <h4 className="mb-1 flex items-center gap-2 text-sm font-semibold">
            ④ 想定効果
            <StatusBadge kind="neutral">
              {summary.impact_source === 'genai' ? 'GenAI 文章化' : '定型文'}
            </StatusBadge>
          </h4>
          <p className="text-sm" data-testid="summary-impact">
            {summary.impact}
          </p>
        </section>
      </div>
    </Panel>
  )
}

export default function Preview() {
  const { sid } = useParams()
  const user = useUser()
  const [comp, setComp] = useState<DemoComposition | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const [gov, setGov] = useState<GovernanceReport | null>(null)
  const [launch, setLaunch] = useState<DemoLaunch | null>(null)
  const [summary, setSummary] = useState<DemoSummary | null>(null)
  const [busy, setBusy] = useState<'' | 'validate' | 'launch' | 'summary'>('')
  const [stepError, setStepError] = useState<string | null>(null)

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

  // 検証/起動/サマリ共通の POST 呼び出し。エラーは detail を抽出して文字列化する。
  // launch の 409 は detail がオブジェクト({message, governance, ...})なので message を拾う。
  async function postStep(path: string): Promise<Record<string, unknown>> {
    const r = await fetch(`/api/hearing/sessions/${sid}${path}`, {
      method: 'POST',
      headers: authHeaders(user),
    })
    if (r.status === 401) {
      reauthenticate()
      throw new Error('unauthorized')
    }
    const body = await r.json().catch(() => ({}))
    if (!r.ok) {
      const detail = (body as { detail?: unknown }).detail
      if (detail && typeof detail === 'object') {
        const d = detail as { message?: string; governance?: GovernanceReport }
        if (d.governance) setGov(d.governance)
        throw new Error(d.message || `エラー (${r.status})`)
      }
      throw new Error((detail as string) || `エラー (${r.status})`)
    }
    return body as Record<string, unknown>
  }

  async function onValidate() {
    setBusy('validate')
    setStepError(null)
    // 再検証は構成を見直す操作なので、前回の起動/サマリ表示は陳腐化し得る。古い起動パネル・サマリが
    // 残らないようクリアしてから検証する(別タブでの回答変更・再推薦後の stale 表示を防ぐ)。
    setLaunch(null)
    setSummary(null)
    try {
      const body = await postStep('/validate')
      setGov(body.governance as GovernanceReport)
      if (body.composition) setComp(body.composition as DemoComposition)
    } catch (e) {
      setStepError(String((e as Error).message ?? e))
    } finally {
      setBusy('')
    }
  }

  async function onLaunch() {
    setBusy('launch')
    setStepError(null)
    try {
      const body = await postStep('/launch')
      setLaunch(body.launch as DemoLaunch)
      if (body.governance) setGov(body.governance as GovernanceReport)
    } catch (e) {
      setStepError(String((e as Error).message ?? e))
    } finally {
      setBusy('')
    }
  }

  async function onSummary() {
    setBusy('summary')
    setStepError(null)
    try {
      const body = await postStep('/summary')
      setSummary(body as unknown as DemoSummary)
    } catch (e) {
      setStepError(String((e as Error).message ?? e))
    } finally {
      setBusy('')
    }
  }

  async function onExport() {
    // エクスポートはサーバの正準エンドポイント(GET /summary/export)を取得してダウンロードする。
    // これにより API/画面/ドキュメントで「再現可能な決定的 Markdown」を一致させる(API が唯一の出力源)。
    setStepError(null)
    try {
      const r = await fetch(`/api/hearing/sessions/${sid}/summary/export`, {
        headers: authHeaders(user),
      })
      if (r.status === 401) {
        reauthenticate()
        throw new Error('unauthorized')
      }
      if (!r.ok) {
        const body = await r.json().catch(() => ({}))
        const detail = (body as { detail?: unknown }).detail
        const msg =
          detail && typeof detail === 'object'
            ? (detail as { message?: string }).message
            : (detail as string)
        throw new Error(msg || `エラー (${r.status})`)
      }
      const md = await r.text()
      const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `demo-summary-${sid}.md`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      setStepError(String((e as Error).message ?? e))
    }
  }

  const canLaunch = comp?.ok === true && gov?.ok === true

  return (
    <PageContainer
      icon="diagram"
      title="デモ構成プレビュー"
      subtitle="推薦から合成したデモ構成を確認し、検証 → 起動 → 顧客提示サマリまで一気通貫で進めます"
    >
      <div className="mb-3 text-sm">
        <Link to="/hearing" className="text-link hover:underline">
          ← ヒアリングへ戻る
        </Link>
      </div>
      {loading && <p className="text-sm text-ink-muted">合成中…</p>}
      {error && (
        <Panel title="読み込みエラー" className="border border-pill-err">
          <p className="text-sm text-pill-err-ink">{error}</p>
        </Panel>
      )}
      {comp && (
        <div className="flex flex-col gap-4">
          <CompositionPreview comp={comp} />

          {comp.ok && (
            <>
              {/* 一気通貫: 検証 → 起動 → サマリ。各ステップは利用者操作で進める。 */}
              <div className="flex flex-wrap items-center gap-2" data-testid="flow-actions">
                <OciButton onClick={onValidate} disabled={busy !== ''}>
                  {busy === 'validate' ? '検証中…' : '構成を検証する'}
                </OciButton>
                <OciButton
                  onClick={onLaunch}
                  disabled={busy !== '' || !canLaunch}
                  title={canLaunch ? '' : '検証 PASS 後に起動できます'}
                >
                  {busy === 'launch' ? '起動中…' : 'このデモを起動'}
                </OciButton>
                <OciButton variant="outline" onClick={onSummary} disabled={busy !== ''}>
                  {busy === 'summary' ? '生成中…' : '構成サマリを生成'}
                </OciButton>
              </div>

              {gov && !gov.ok && (
                <p className="text-xs text-ink-muted" data-testid="launch-blocked-note">
                  ⚠ 検証 FAIL の構成は起動できません。下の代替提案に従って構成を直してください。
                </p>
              )}

              {stepError && (
                <Panel title="操作エラー" className="border border-pill-err">
                  <p className="text-sm text-pill-err-ink">{stepError}</p>
                </Panel>
              )}

              {gov && <ValidationPanel gov={gov} />}
              {launch && <LaunchPanel launch={launch} />}
              {summary && <SummaryPanel summary={summary} onExport={onExport} />}
            </>
          )}
        </div>
      )}
    </PageContainer>
  )
}
