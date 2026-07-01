# タスク: EXB-05 Typed Action Client（answer.with-citations@1 専用・生 URL 非露出）

## ゴール
生成 UI が Action を型安全に消費する**薄い TypeScript クライアント**を実装する（`useJetUseAction` 相当）。
`answer.with-citations@1` 専用。`start()` で Run を開始し `events()` で標準 RunEvent を購読する。
**生の API URL を UI に露出しない**（実装方針 §11.1）。

## 対象 area
web

## 前提（Stage 0 契約 ＋ EXB-03 の Run API）
- 入出力/イベント型は Stage 0 スキーマ（`answer-with-citations.input/output/event` ＋ `run-event`）に対応する
  TS 型で表す（スキーマから手写し最小で可・単一の真実源はスキーマ）。
- 呼ぶ API は EXB-03 の Run API（`POST .../actions/{actionId}/runs`・`GET /runs/{id}/events` SSE）。

## 受け入れ条件（検証可能な述語で書く）
- [ ] `packages/web` 内に型付きクライアントを置く（MVP。将来 `packages/runtime-sdk` へ分離するため、
  UI コンポーネントから API パス/URL を直接組み立てさせない薄い境界にする）。
- [ ] API: `start(input): Promise<{runId}>` と `events(runId): AsyncIterable<RunEvent>`（SSE 購読）を提供。
  もしくは `run(input)` で開始＋イベントストリームを返す一体型でもよい（生 URL を隠すのが要件）。
- [ ] `answer.with-citations@1` 専用の型: `input={question, conversationId?}` / 最終 `output={answer, citations[]}` /
  イベントは標準語彙（`message.delta`/`retrieval.completed` 等）に型付け。
- [ ] 生 API URL/パスを UI に露出しない（クライアント内部に隠蔽・`actionId` は論理名で受ける）。
- [ ] テスト: モック SSE に対し `events()` が RunEvent を順に yield し、`message.delta` の逐次結合と
  `run.completed` の output を取り出せる。型チェック通過。`npm run build` 成功（CLAUDE.md コミット前チェック）。

## E2E シナリオ（実環境 / jetuse-dev）
- [ ] シナリオ1: EXB-03 の Run API（スタブまたは実 Provider）に対しクライアント経由で run→イベント購読→
  引用付き回答を取得できることを確認・証跡記録（`runs/<run-id>/e2e/`）。UI は API URL を知らずに SDK 経由で消費。
- [ ] 実 Provider（EXB-04）が統合済みなら実 RAG に対して、未統合ならスタブ Provider に対して実行し、状況を明記。

## 非ゴール / 制約
- 汎用 SDK / 複数 Action 対応 / コード生成を作らない（MVP は `answer.with-citations@1` 専用・薄い型付け）。
- 手書き UI 画面は作らない（本タスクはクライアント層。Action→Run→引用が SDK 経由で取れる状態が完了条件）。
- 新規依存を安易に足さない（SSE は `EventSource`/fetch stream 等ネイティブ優先）。
- spec-driven: SDK 形状で仕様外判断が要るなら実装せず ADR 案を残す。

## 依存
EXB-01（型の元スキーマ）・EXB-03（Run API）。
