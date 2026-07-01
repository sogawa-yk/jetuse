# タスク: EXB-03 Run/RunEvent/Artifact モデル ＋ Action/Run API（rag.answer 限定・SSE）

## ゴール
Experience の Action を実行する **Run API** を最小実装する。標準 Run イベント語彙（Stage 0 の
`run-event.schema.json`）を SSE で流し、`answer.with-citations@1`（`rag.answer`）の実行状況・引用・回答を
RunEvent として返す。既存 API（`/api/chat` 等）は壊さない。実装方針 §7.4（Run/Action API）。

## 対象 area
api

## 前提（Stage 0 契約・正本）
- 標準イベント語彙: `packages/api/jetuse_platform/contracts/schemas/run-event.schema.json`
  （`run.started` / `message.delta` / `retrieval.started` / `retrieval.completed` / `run.completed` /
  `run.failed` / `run.cancelled` / `artifact.created` ほか）。`run_id`/`type`/`seq`/`ts` 必須・`seq` は 0 起点で単調増加。
- Capability 固有 payload: `answer-with-citations.event.schema.json`（`message.delta.data.text` /
  `retrieval.completed.data.citations[]`）。入出力: `answer-with-citations.input/output.schema.json`。
- Descriptor: `reference_descriptors/descriptors/rag-answer.json`（`action: answer.with-citations@1`・`executionMode: stream`）。

## 受け入れ条件（検証可能な述語で書く）
- [ ] Run/RunEvent/Artifact のモデルを定義（Pydantic）。RunEvent は `run-event.schema.json` に**準拠**（バリデータで検証）。
- [ ] 最小の Run ストア（in-process・in-memory で可。MVP は永続化しない）。`run_id` 採番・`seq` 単調増加・状態遷移
  （`queued`→`running`→`completed`/`failed`/`cancelled`）。
- [ ] FastAPI ルート（既存 `catalog.py` の規約に合わせる。`require_user` 認証必須）:
  - `POST /api/v1/experiences/{experience_id}/actions/{action_id}/runs` — Run を開始し `run_id` を返す。
    `action_id` は `answer.with-citations@1` のみ受理（未知は 400/404）。入力は `input` スキーマで検証。
  - `GET /api/v1/runs/{run_id}` — Run の状態を返す（404 で未知）。
  - `GET /api/v1/runs/{run_id}/events` — **SSE** で RunEvent を `seq` 順に配信（`text/event-stream`）。
  - `GET /api/v1/runs/{run_id}/artifacts` — 生成 Artifact 一覧（MVP は空配列で可）。
- [ ] **Provider seam**: Run 実行は Provider を介す薄い interface（Protocol）で分離し、実 RAG は EXB-04 が差す。
  本タスクは seam＋**スタブ Provider**（既知 question にダミー回答＋引用を返す）で Run/SSE 経路を独立に検証する。
  OCI を直叩きしない（実装方針 §3.5 / §12.2。実 RAG 接続は EXB-04）。
- [ ] SSE のイベント列が語彙どおり: `run.started` → `retrieval.started` → `retrieval.completed`(引用) →
  `message.delta`(逐次) → `run.completed`(output=answer+citations)。異常時は `run.failed`。
- [ ] 単体/結合テスト: run 開始→`GET /runs/{id}` 状態・SSE イベント順序と schema 準拠・未知 action は 4xx・
  不正 input は 422・未知 run_id は 404。ruff / 型 / `pytest packages/api/tests` クリーン。既存契約を壊さない
  （`main.py` の既存ルート・テスト回帰なし）。

## E2E シナリオ（実環境 / jetuse-dev）
本タスクはスタブ Provider のため OCI 実リソースに触れない（実 RAG 引用の E2E は EXB-04）。
- [ ] シナリオ1: TestClient で run 開始→SSE を購読し、標準語彙のイベント列＋`run.completed` の output が
  `answer-with-citations.output` に準拠することを確認・証跡記録（`runs/<run-id>/e2e/`）。
- [ ] シナリオ2: 未知 action / 不正 input / 未知 run_id の異常系を確認。
- [ ] 実 OCI 到達面が無いため `runs/<run-id>/e2e/SKIPPED.md` に理由明記（実 RAG 引用は EXB-04 で実施）。

## 非ゴール / 制約
- 汎用 Workflow Runtime / 永続 Run ストア / 複数 Capability 対応を作らない（MVP は `rag.answer` 縦切り1本・
  実装方針 §15）。Run ストアは in-memory で十分。
- 実 RAG 実行・OCI 直呼びをしない（EXB-04 の Provider Adapter が seam に入る）。
- spec-driven: イベント語彙や API 形状で仕様外判断が要るなら実装せず `docs/decisions/` に ADR 案を残す。

## 依存
Stage 0（契約スキーマ・Descriptor）。EXB-04 とは Provider seam（＝Stage 0 の output/event 契約）で疎結合。
