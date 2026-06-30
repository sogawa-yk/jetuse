# タスク: EXB-02 RAG Reference Implementation Descriptor（静的）＋ Catalog ローダー

## ゴール
main に実機検証済みの RAG を、Builder が安全に選べる **機械可読な Reference Implementation Descriptor** として
記述する。MVP では汎用 Catalog サービスを作らず、**リポジトリ内の静的 Descriptor** とする
（実装方針 §3.6 / §7.1 / コンセプト §6.2）。

## 対象 area
api

## 受け入れ条件（検証可能な述語で書く）
- [ ] `packages/api/jetuse_platform/reference_descriptors/`（新設）に `rag.answer` の静的 Descriptor を置く。
  最低限、次を含む（実装方針 §7.1 / コンセプト §20 用語）:
  - `id` / `version`（`answer.with-citations@1` を含む）/ `displayName` / `executionMode: stream`
  - `configSchema` / `inputSchema` / `outputSchema` / `eventSchema`（EXB-01 のスキーマを参照）
  - `supportedScenarios`（例: `support-answer-with-citations`）/ `experienceChannels: [web]`
  - `limitations`（本番性能・可用性・顧客固有精度は未検証）/ `handoffTriggers`（専任チーム引き継ぎ条件）
- [ ] Descriptor を読み込む **静的 Catalog ローダー**を実装する（in-process。実装方針 §5.1「Reference 一覧＝静的
  Descriptor / Action 解決＝単純な対応表」）。FastAPI の読取ルート（`GET /api/v1/catalog/capabilities` /
  `.../{id}/versions/{version}` 相当）を**最小**で公開してよい。
- [ ] ローダー/ルートの単体テスト: 既知 id/version の Descriptor を取得できる、未知 id は 404/エラーになる、
  Descriptor が EXB-01 のスキーマ語彙と整合する（参照 schema 名が実在する）。
- [ ] 存在しない Action/Capability を参照していない（Descriptor が宣言する schema/scenario が実在する）。
- [ ] ruff / 型チェック / `pytest packages/api/tests` クリーン。既存契約を壊さない。

## E2E シナリオ（実環境 / jetuse-dev・複数）
静的 Descriptor＋in-process ローダーであり、OCI 実リソース面に触れない。
- [ ] シナリオ1: FastAPI TestClient で `GET /api/v1/catalog/...` が Descriptor を 200 で返すことを確認し証跡記録。
- [ ] シナリオ2: 未知 id/version でエラー（404 等）になることを確認。
- [ ] 実環境到達面が無いため `runs/<run-id>/e2e/SKIPPED.md` に理由明記（実 RAG 接続は EXB-04 で実施）。

## 非ゴール / 制約
- Catalog を**サービス化しない**（Gate 成立後。実装方針 §7.1 末尾）。Descriptor は静的ファイル＋薄いローダー。
- 実 RAG への接続・実行はこのタスクでしない（EXB-04 の Provider Adapter）。
- 複数 Capability（OCR/NL2SQL/Agent 等）の Descriptor を一括追加しない（MVP は `rag.answer` のみ。実装方針 §8.2）。
- spec-driven: Descriptor の項目設計で仕様外判断が要るなら ADR 案を残す。

## 依存
EXB-01（参照する JSON Schema）。
