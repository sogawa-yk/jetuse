# 17. Experience Builder — MVP 契約スキーマ

正本ドキュメント: `docs/architecture/experience-builder-implementation-strategy.md`
（§6.1 Experience 構造 / §6 ドメインモデル / §7.1 Capability Descriptor / §7.4 標準 Run イベント語彙）。

本書は MVP（引用付き RAG の縦切り 1 本: `answer.with-citations@1`）に必要な最小範囲の
JSON Schema（Draft 2020-12）の**仕様説明**である。スキーマ本体（実体）は実装パッケージに
同梱され、`pip install` で wheel / イメージに入る。

- スキーマ実体（機械可読・単一の真実源）: `packages/api/jetuse_platform/contracts/schemas/*.json`
  （配布 wheel/イメージへ同梱するためバリデータと同居。本書はその仕様文書）
- 検証コード: `packages/api/jetuse_platform/contracts/`（`jsonschema` で読み込み・検証）

spec-driven は維持する。実装判断を要する設計はここに記述し、スキーマ JSON はパッケージ同梱
（`importlib.resources` で読み込み）として運用する（`specs/` からの相対パス参照に依存しない）。

## スキーマ一覧（`packages/api/jetuse_platform/contracts/schemas/`）

| ファイル | 対象 | 根拠 |
|---|---|---|
| `experience.schema.json` | Experience 定義（metadata/ui/channels/resources/actions） | §6.1 |
| `demo-bundle.schema.json` | DemoBundle（再現可能な固定構成・データ版・Action Binding・Quality Gate） | §6 / §3.8 |
| `demo-evidence-pack.schema.json` | DemoEvidencePack（動作構成・顧客確認・制約・未検証事項・引き継ぎ） | §6 / §16.5 |
| `answer-with-citations.config.schema.json` | `answer.with-citations@1` の **configSchema**（Builder が束縛） | §7.1 |
| `answer-with-citations.input.schema.json` | 同 **inputSchema**（実行時に UI が渡す） | §7.1 |
| `answer-with-citations.output.schema.json` | 同 **outputSchema**（answer + citations） | §7.1 |
| `answer-with-citations.event.schema.json` | 同 **eventSchema**（message.delta / retrieval.completed 等の固有 payload） | §7.1 |
| `run-event.schema.json` | 標準 Run イベント語彙の共通エンベロープ（run_id/type/seq/ts + data） | §7.4 |

## なぜ config と input を分けるのか

`answer.with-citations@1` は **configSchema** と **inputSchema** を別スキーマにしている。

- **config（Builder が束縛）**: Knowledge Space 参照や retrieval profile など、案件ごとに
  プリセールスエンジニアが Builder で一度固定する設定。生成 UI（Demo User 向け）には
  毎回指定させない。
- **input（実行時に UI が渡す）**: `question` のように、Demo User がそのつど与える値だけ。

分離することで、生成 Web UI / Slack Channel Adapter は論理 Action（例 `answer-customer`）に
`question` を渡すだけでよく、Knowledge の選択や検索設定を UI 側に漏らさない（§3.1 / §7.1）。
`input` スキーマは `additionalProperties: false` とし、`knowledge` などの config を混入させると
弾く（テストで担保）。

## 標準 Run イベント語彙（§7.4）

`run-event.schema.json` の `type` enum が語彙の正本:
`run.started` / `message.delta` / `retrieval.started` / `retrieval.completed` /
`tool.started` / `tool.completed` / `approval.required` / `artifact.created` /
`run.completed` / `run.failed` / `run.cancelled`。

標準イベントは実行状況のみを表し、Capability 固有データは `data` または各 Descriptor の
`eventSchema` / `outputSchema` で表す。Python 側 `RUN_EVENT_TYPES`（`run_event_types()`）は
この enum を遅延読み込みして公開し、未知 type は schema 検証で弾く。`ts` は `format: date-time`
（RFC3339/ISO8601）で実検証する。

## 範囲外（MVP では作らない）

汎用 Catalog / Resolver / Workflow / Agent のスキーマ、API ルート、永続化、Provider 実装は
本タスク（EXB-01）の範囲外。後続タスクで追加する。
