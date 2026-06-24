/** SPIKE-07ギャラリー(温存) + OCIコンソール部品(UI-02)。トークン確認用 */
import { useState } from 'react'
import {
  Button, Card, ChatBubbles, FormParts, SectionTitle, Table, Toast,
} from '../components/gallery'
import { PageContainer } from '../components/layout'
import {
  Breadcrumbs, DataTable, FeatureCard, LinkCard, OciButton, Panel, StatusBadge, TabBar,
  type Column,
} from '../components/oci'

// 指定パレット(docs/feedbacks/colorcode.md)。デザインギャラリーで実値を提示する
const PALETTE = [
  {
    bg: { name: 'Light background · Neutral 10', hex: '#FBF9F8' },
    ink: '#2A2F2F',
    accents: [
      { name: 'Sky 140', hex: '#04536F' },
      { name: 'Rose 140', hex: '#6C3F49' },
      { name: 'Oracle Red', hex: '#C74634' },
    ],
  },
  {
    bg: { name: 'Dark background · Slate 170', hex: '#2A2F2F' },
    ink: '#FBF9F8',
    accents: [
      { name: 'Brand 170', hex: '#F0CC72' },
      { name: 'Teal 79', hex: '#89B2B0' },
      { name: 'Pine 70', hex: '#86B596' },
    ],
  },
] as const

function ColorPalette() {
  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
      {PALETTE.map((g) => (
        <div
          key={g.bg.hex}
          className="rounded-rw-lg p-4 shadow-rw"
          style={{ background: g.bg.hex, color: g.ink }}
        >
          <div className="mb-3 text-sm font-bold">
            {g.bg.name}{' '}
            <span className="font-mono font-normal opacity-70">{g.bg.hex}</span>
          </div>
          <div className="flex flex-wrap gap-3">
            {g.accents.map((c) => (
              <div key={c.hex} className="text-xs">
                <div
                  className="h-12 w-20 rounded-rw border border-black/10"
                  style={{ background: c.hex }}
                />
                <div className="mt-1 font-medium">{c.name}</div>
                <div className="font-mono opacity-70">{c.hex}</div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

type DemoRow = { name: string; type: string; status: 'ok' | 'warn'; when: string }

const DEMO_ROWS: DemoRow[] = [
  { name: 'jetuse-dev-adb', type: 'Autonomous AI Database', status: 'ok', when: '1時間前' },
  { name: 'jetuse-dev-api', type: 'Container Instance', status: 'ok', when: '2時間前' },
  { name: 'jetuse-dev-project', type: 'AI Project', status: 'warn', when: '2時間前' },
  { name: 'jetuse-dev-app-spa', type: 'Bucket', status: 'ok', when: '1日前' },
]

const DEMO_COLS: Column<DemoRow>[] = [
  {
    key: 'name',
    label: '名前',
    render: (r) => (
      <span className="cursor-pointer font-medium text-action hover:underline">{r.name}</span>
    ),
  },
  { key: 'type', label: 'タイプ' },
  {
    key: 'status',
    label: '状態',
    render: (r) => (
      <StatusBadge kind={r.status}>{r.status === 'ok' ? 'アクティブ' : '進行中'}</StatusBadge>
    ),
  },
  { key: 'when', label: '作成時間', className: 'text-ink-muted' },
]

function OciShowcase() {
  const [tab, setTab] = useState('tags')
  return (
    <div className="space-y-4">
      <Panel>
        <div className="space-y-3">
          <Breadcrumbs
            items={[
              { label: 'ホーム', to: '/' },
              { label: 'デザインギャラリー', to: '/design' },
              { label: 'OCIコンソール部品' },
            ]}
          />
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="mr-2 text-2xl font-bold">jetuse-dev-adb</h2>
            <StatusBadge kind="ok">アクティブ</StatusBadge>
          </div>
          <div className="flex flex-wrap gap-2">
            <OciButton caret>データベース・アクション</OciButton>
            <OciButton variant="outline">データベース接続</OciButton>
            <OciButton variant="outline" caret>その他のアクション</OciButton>
            <OciButton variant="ghost">キャンセル</OciButton>
            <OciButton disabled>無効</OciButton>
          </div>
          <TabBar
            tabs={[
              { key: 'info', label: '一般情報' },
              { key: 'tags', label: 'タグ' },
              { key: 'metrics', label: 'メトリック' },
            ]}
            active={tab}
            onChange={setTab}
          />
          <div className="flex flex-wrap gap-2 text-sm text-ink-muted">
            <StatusBadge kind="ok">アクティブ</StatusBadge>
            <StatusBadge kind="warn">進行中</StatusBadge>
            <StatusBadge kind="err">失敗</StatusBadge>
            <StatusBadge kind="neutral">停止済み</StatusBadge>
            <span className="self-center">(選択中タブ: {tab})</span>
          </div>
        </div>
      </Panel>
      <Panel title="リソース" action={<OciButton variant="ghost">すべてのリソースの表示</OciButton>}>
        <DataTable columns={DEMO_COLS} rows={DEMO_ROWS} rowKey={(r) => r.name} />
      </Panel>
      {/* カード(screen-example-3準拠): 大=色面ヘッダ、小=右アイコンチップ */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <FeatureCard
          to="/design"
          icon="🧭"
          tone="terracotta"
          title="Redwood Reference Application"
          desc="Redwoodプラットフォームの機能とコンポーネントの使い勝手を確認できます。"
        />
        <FeatureCard
          to="/design"
          icon="🌲"
          tone="green"
          title="The Redwood story"
          desc="全体的なアプローチとデザインの考え方を体験できるビデオストーリー。"
        />
      </div>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <LinkCard to="/design" icon="🧱" title="Develop for Redwood" desc="Redwoodのデザイン目標とプラットフォーム原則に沿ったアプリケーションを構築。" />
        <LinkCard to="/design" icon="🎨" title="Design for Redwood" desc="Redwood Design Systemを探索し、優れた利用体験の作り方を学ぶ。" />
        <LinkCard to="/design" icon="📘" title="Redwood Pattern Book" desc="Redwoodページの構築例を確認。" badge="共有" />
      </div>
    </div>
  )
}

export default function Design() {
  const [toast, setToast] = useState<string | null>(null)
  return (
    <PageContainer icon="design" title="デザインギャラリー（SPIKE-07）">
      <div className="space-y-8">
        <section>
          <SectionTitle>カラーパレット（colorcode.md）</SectionTitle>
          <ColorPalette />
        </section>
        <section>
          <SectionTitle>OCIコンソール部品（UI-02）</SectionTitle>
          <OciShowcase />
        </section>
        <section>
          <SectionTitle>ボタン</SectionTitle>
          <Card>
            <div className="flex flex-wrap items-center gap-3">
              <Button>プライマリ</Button>
              <Button variant="secondary">セカンダリ</Button>
              <Button variant="ghost">ゴースト</Button>
              <Button variant="danger">削除</Button>
              <Button disabled>無効</Button>
            </div>
          </Card>
        </section>
        <section>
          <SectionTitle>チャットバブル（SSEストリーミング表示の想定）</SectionTitle>
          <Card>
            <ChatBubbles />
          </Card>
        </section>
        <section>
          <SectionTitle>フォーム部品</SectionTitle>
          <Card>
            <FormParts onSubmit={() => setToast('フォームを送信しました（デモ）')} />
          </Card>
        </section>
        <section>
          <SectionTitle>テーブル</SectionTitle>
          <Card>
            <Table />
          </Card>
        </section>
      </div>
      {toast && <Toast message={toast} onClose={() => setToast(null)} />}
    </PageContainer>
  )
}
