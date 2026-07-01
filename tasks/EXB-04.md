# タスク: EXB-04 RAG Provider Adapter（jetuse_core 委譲・main 回帰比較）

## ゴール
`answer.with-citations@1`（`rag.answer`）の **実 RAG 実行**を、`jetuse_core` の実機検証済み RAG に**委譲**する
Provider Adapter として実装する。生成 UI / 新 API から OCI を直叩きせず、Adapter 経由で再利用する
（実装方針 §3.5 / §12.2）。出力は Stage 0 の `outputSchema`（answer+citations）・イベントは標準 Run 語彙 ＋
`answer-with-citations.event` に整形する。

## 対象 area
api

## 前提（Stage 0 契約・正本）
- 出力契約: `answer-with-citations.output.schema.json`（`answer` ＋ `citations[]{source,score?,snippet?}`）。
- イベント契約: 標準 `run-event.schema.json` ＋ `answer-with-citations.event.schema.json`
  （`retrieval.started` / `retrieval.completed{citations}` / `message.delta{text}`）。
- config 契約: `answer-with-citations.config.schema.json`（`knowledge.space`(+version) / `retrieval.topK`）。
- 委譲先: 既存 `jetuse_core` の RAG（`packages/api/service/routes/rag.py` が使う実装／file_search 委譲）。
  既存 RAG を**書き直さない**。ADR-0021 の seam 方式（既存資産の再利用）に倣う。

## 受け入れ条件（検証可能な述語で書く）
- [ ] `packages/api/jetuse_platform/providers/rag_answer/`（新設）に Provider Adapter を実装する。
  EXB-03 の Provider seam（Protocol）を満たす（無ければ本タスクで seam を定義し EXB-03 と契約整合させる）。
- [ ] Adapter は config（`knowledge.space` / `retrieval.topK`）と input（`question`）を受け取り、`jetuse_core` の
  実 RAG を呼び、**ストリーミング**で標準イベントを発行する: `retrieval.started` → `retrieval.completed`(citations) →
  `message.delta`(逐次 text) → 完了で `outputSchema` 準拠の `{answer, citations}` を返す。
- [ ] citations の `source` は引用元（例 `doc#pN`）を必ず含める。ヒット無し（Empty）時は空 citations ＋
  「該当なし」系の回答で正常終了する（`run.failed` にしない）。
- [ ] OCI を直叩きしない（Adapter は `jetuse_core` の RAG 経由のみ）。認証・エンドポイントは既存経路を再利用。
- [ ] 単体/結合テスト: config/input バリデーション・イベント順序と schema 準拠・Empty 経路。
  ruff / 型 / `pytest packages/api/tests` クリーン。既存 RAG ルート・テストに回帰なし。

## E2E シナリオ（実環境 / jetuse-dev・複数）
実 RAG（実 Knowledge）に対して実行する。証跡は `runs/<run-id>/e2e/`。
- [ ] シナリオ1（ヒットあり）: 実 Knowledge に既知の質問 → 引用付き回答が得られ、`source` が実在文書を指す。
- [ ] シナリオ2（Empty）: 該当しない質問 → 空 citations で正常終了（例外にしない）。
- [ ] シナリオ3（main 回帰比較）: 同一質問を **main 由来の RAG 経路**（既存 `/api/...` RAG）と Adapter 経由で実行し、
  回答/引用が実質同等（委譲で退行していない）ことを確認・証跡記録。差異があれば原因を記す。
- [ ] 使用した Knowledge/接続情報は実値をコミットしない（`.env`。CLAUDE.md）。

## 非ゴール / 制約
- 新しい RAG エンジンや検索基盤を作らない（既存 `jetuse_core` を委譲再利用）。
- 複数 Capability（OCR/NL2SQL/Agent）の Adapter を作らない（MVP は `rag.answer` のみ・実装方針 §8.2）。
- Run 転送/SSE ルートは EXB-03 の担当（本タスクは Provider ロジックとイベント整形）。
- spec-driven: 委譲境界や config 解釈で仕様外判断が要るなら実装せず ADR 案を残す。

## 依存
EXB-02（RAG Descriptor / 参照契約）。Provider seam で EXB-03 と契約整合（統合時に配線を検証）。
