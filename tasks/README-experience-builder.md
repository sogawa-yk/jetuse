# Experience Builder ステージ計画（stage-runner 用ロードマップ）

正本ドキュメント:
- [プロダクトコンセプト](../docs/architecture/jetuse-product-concept.md)
- [実装方針（main安定版から）](../docs/architecture/experience-builder-implementation-strategy.md)
- [AIアプリビルダー初期構想](../docs/architecture/ai-application-builder-vision.md)（参考・初期検討）

## 大方針（docs の第一仮説）
> コーディング/AIアーキテクチャ/UIに詳しくないプリセールスが、**main に実機検証済みの OCI AI
> リファレンス実装**を正しく使い、顧客が業務適合性を評価できる Web/SaaS デモを完成できる。

このため **MVP は「引用付き RAG（`answer.with-citations@1`）の縦切り1本」に限定**する。汎用 Catalog /
Resolver / Workflow Runtime / Hosted Runtime / Marketplace は **Gate が成立するまで作らない**
（実装方針 §15 / コンセプト §17）。各ステージは「広く作る」のではなく「縦に薄く通す」。

## 根ブランチ
docs では `next/experience-builder` を v2 統合の根としているが、本リポジトリでは **`dev`**（main 派生・
ループ方法論を載せたブランチ）を根とする。stage-runner / loop の既定 base は `dev`
（`begin_stage.sh` 既定・`loop-config.yml` `worktree.base_branch`）。

- 根（base）: `dev`
- ステージ統合（自動 commit+merge・隔離・push しない）: `feat/stage-<N>`（dev から分岐・throwaway local）
- タスク worktree: `feat/<task>`（dev または前段から分岐）

## ステージ全体像（Gate / Phase との対応）

| Stage | 目的（縦切り） | 対応（実装方針 §14 / コンセプト §17） | 状態 |
|---|---|---|---|
| **Stage 0** | 契約とベースライン確定 | Phase 0 / Gate 0 準備 | **チケット済み・実行可** |
| **Stage 1** | RAG Action 縦切りバックエンド | Phase 1+2 の `rag.answer` のみ | 計画（Stage 0 完了後にチケット化） |
| **Stage 2** | 引用付き Web Experience | Gate 1（Inbox+Detail / Answer+Citations / Quality Gate） | 計画 |
| **Stage 3** | Slack Hybrid | Gate 3 | 計画 |
| **Stage 4（後続）** | **API 統合リファクタリング** | Action/Run API を canonical 化・既存 UI も移行・旧個別ルート段階廃止（ADR-0022 §4） | 計画（縦切り安定後） |

> **API 統合フェーズ（施主指摘 2026-07-01）**: MVP は Provider Adapter で速く通すが、その後 Action/Run API を
> **正式な JetUse API に昇格**させ、**既存 UI とビルダー生成 UI が同じ統合 API を呼ぶ**形へ寄せる
> リファクタリングを明示的に挟む（AI ロジックは書き直さず、API 層のみ統合）。詳細は ADR-0022 §4。

> **なぜ先に全部チケット化しないか**: docs は「Gate を満たすまで汎用基盤を拡張しない」と明言。
> 先のステージの詳細は前段の契約が固まってから書く（over-spec を避ける＝ponytail / docs §14 と整合）。
> Stage 1 以降は本ファイルの概要＋ `STAGE<N>-PROGRESS.md` のキュー表まで。詳細チケットは前段 done 後に追加。

## 各ステージの中身（概要）

### Stage 0 — 契約とベースライン（実行可）
`tasks/STAGE0-PROGRESS.md` 参照。
- **EXB-00**: dev を v2 起点に確定。main 由来の既存テストが dev で全件パス（回帰ベースライン）。ADR-0022
  （Experience Builder の方針・dev 根・MVP=RAG縦切り・汎用基盤は Gate まで作らない）を起票。
- **EXB-01**: MVP 契約スキーマ（JSON Schema）= `Experience` / `DemoBundle` / `answer.with-citations@1`
  （config/input/output/event）＋標準 Run イベント語彙。
- **EXB-02**: RAG Reference Implementation Descriptor（リポジトリ内静的）＋静的 Catalog ローダー。

### Stage 1 — RAG Action 縦切りバックエンド（計画）
- **EXB-03**: Run / RunEvent / Artifact 最小モデル＋ Action/Run API（`rag.answer` 限定。SSE イベント）。
- **EXB-04**: RAG Provider Adapter（`jetuse_core` の実 RAG を委譲。OCI 直呼びしない。main 回帰比較）。
- **EXB-05**: Typed Action Client（`answer.with-citations@1` 専用の薄い TS クライアント。生 URL を露出しない）。

### Stage 2 — 引用付き Web Experience（計画）
- 制約 Redwood Experience Pattern（Inbox+Detail / Answer+Citations）／FixtureSet・KnowledgeSpace 最小／
  Quality Gate・Preflight（実RAG接続・引用・Streaming/Loading/Empty/Error/Retry の自動確認）。

### Stage 3 — Slack Hybrid（計画）
- Slack Reference Channel Adapter（認証/Event/再送/エラーを固定）／Slack→Web 詳細遷移／同一 RAG Action 共有。

## 回し方（stage-runner）
```bash
# 例: Stage 0 を回す（既定 base=dev。feat/stage-0 を dev から分岐して自動統合）
.claude/loop/start-stage.sh stage-0
# 起動セッションで /stage-runner を実行 → キュー枯渇まで自走 → runs/_stages/stage-0/REPORT.md で1回報告
```
- 自動統合は `feat/stage-0` 限定。push / base(dev) への PR / apply / ADR 承認 / IAM は自走中も停止（人間ゲート）。
- 各タスクは Codex PASS＋test/lint＋実環境 E2E（`runs/<run-id>/e2e/`）で done。詳細は `docs/loop-engineering.md`。

## 非ゴール（MVP 全体・docs §15 / コンセプト §15）
汎用 Catalog/Resolver サービス、Workflow Runtime、Agent/OCR/NL2SQL の網羅、Hosted Runtime、Marketplace、
Slack 以外の SaaS、任意 SaaS Connector 動的生成、Dify 相当キャンバス、顧客セルフサービス Builder、
生成UIからの OCI SDK / 既存個別 API 直叩き。これらは Gate 成立を実証してから判断する。
