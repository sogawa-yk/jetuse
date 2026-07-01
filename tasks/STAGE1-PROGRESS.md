# ステージ1 進捗キュー（計画 — Stage 0 完了後にチケット化）

Experience Builder の第二ステージ＝**RAG Action 縦切りバックエンド**（`rag.answer` のみ。Phase 1+2 の RAG 部分）。
base=`dev`、ステージ統合 `feat/stage-1`。

> ✅ **実行可能**（2026-07-01）。Stage 0（契約）は done で `dev` にマージ済み（PR #29）。確定した Run イベント語彙
> （`run-event.schema.json`）・スキーマ（`answer-with-citations.*`）・Descriptor（`rag-answer.json`）に基づき、
> 詳細チケット `tasks/EXB-03/04/05.md` を起票済み。stage-runner が `feat/stage-1` へ自走統合する。

status: `todo` | `in_progress` | `blocked` | `done`

依存解決: EXB-03(dep=Stage 0✓) と EXB-04(dep=EXB-02✓) は **第1波で並行**。EXB-05(dep=EXB-01✓,EXB-03) は EXB-03 done 後の第2波。

| 順 | タスク | 依存 | 人間ゲート | status |
|---|---|---|---|---|
| 1 | EXB-03 Run/RunEvent/Artifact モデル＋ Action/Run API（rag.answer 限定・SSE） | Stage 0 | コミット | todo |
| 2 | EXB-04 RAG Provider Adapter（jetuse_core 委譲・OCI 直呼びしない・main 回帰比較） | EXB-02 | コミット・実OCI接続 | todo |
| 3 | EXB-05 Typed Action Client（answer.with-citations@1 専用・生URL非露出） | EXB-01, EXB-03 | コミット | todo |

## 予定スコープ（概要・正本は Stage 0 確定後のチケット）
- **EXB-03**: `POST /api/v1/experiences/{id}/actions/{actionId}/runs` / `GET /runs/{id}` /
  `GET /runs/{id}/events`(SSE) / `GET /runs/{id}/artifacts`。標準イベント語彙を発行。`rag.answer` のストリーミングと
  引用を RunEvent で返す。既存 `/api/chat` 等は壊さない。E2E: 実 jetuse-dev で run→SSE 受信→引用付き回答＋異常系。
- **EXB-04**: `jetuse_platform/providers/rag_answer/`。`jetuse_core` の実 RAG を Provider Adapter から委譲
  （生成UI/新APIから OCI を直叩きしない・実装方針 §3.5/§12.2）。`outputSchema`(answer+citations) に整形。
  E2E: 実 Knowledge に対し回答＋引用（ヒットあり／Empty）。main 由来 RAG との回帰比較。
- **EXB-05**: `answer.with-citations@1` 専用の薄い型付き TS クライアント（`useJetUseAction` 相当）。`start()→events()`。
  生 API URL を UI に露出しない（実装方針 §11.1）。MVP は packages/web 内に置き、将来 `packages/runtime-sdk` へ分離。

## 完了条件
3タスク Codex PASS＋test/lint＋実環境 E2E（または理由付き SKIPPED）。手書きUIなしでも Action→Run→引用付き回答が
実 RAG で取得でき、UI は生 API URL を知らずに SDK 経由で消費できる状態。
