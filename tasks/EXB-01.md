# タスク: EXB-01 MVP 契約スキーマ（JSON Schema）

## ゴール
MVP の主要契約を JSON Schema として確定する。サービス実装はまだ作らず、**契約（型）だけ**を先に固める
（実装方針 §3.1「共通化するのは実行契約」/ §7 / コンセプト §6.2）。

## 対象 area
api

## 受け入れ条件（検証可能な述語で書く）
- [ ] `specs/17-experience-builder/` に次の JSON Schema の**仕様**を定義する（spec-driven の正本＝仕様の所在）。
  機械可読 `.json` の実体は配布同梱のため実装パッケージに置く（**ADR-0023・施主承認 2026-06-30**）:
  - `Experience`（実装方針 §6.1 の YAML 構造に対応: metadata / ui / channels / resources / actions / bindings）。
  - `DemoBundle` と `DemoEvidencePack`（再現可能な構成・データ Version・検証結果・未検証事項を含む最小形）。
  - `answer.with-citations@1` の **configSchema / inputSchema / outputSchema / eventSchema**
    （実装方針 §7.1。config と input を分ける＝Knowledge 等は Builder が束縛し UI に毎回指定させない）。
- [ ] 標準 Run イベント語彙を列挙・定義する（実装方針 §7.4）:
  `run.started` / `message.delta` / `retrieval.started` / `retrieval.completed` / `tool.started` /
  `tool.completed` / `approval.required` / `artifact.created` / `run.completed` / `run.failed` / `run.cancelled`。
- [ ] スキーマを読み込んで検証するバリデータ（`jsonschema` 等の既存依存を利用。新規依存を増やさない）と、
  その単体テスト（正例が通り、必須欠落・型不一致の異常例が弾かれる）を `packages/api/tests` に置く。
- [ ] 既存の公開シグネチャ（main 由来 API レスポンス契約）を壊さない。
- [ ] ruff / 型チェック / `pytest packages/api/tests` がクリーン。

## E2E シナリオ（実環境 / jetuse-dev・複数）
スキーマは純データであり、OCI 実リソース面に触れない。
- [ ] シナリオ1（正例）: 代表的な Experience / answer.with-citations の例がスキーマ検証を通ることをユニットで確認し、
  結果を `runs/<run-id>/e2e/` に記録。
- [ ] シナリオ2（異常系）: 必須項目欠落・不正 enum がバリデータで拒否されることを確認。
- [ ] 実環境到達面が無いため `runs/<run-id>/e2e/SKIPPED.md` に理由を明記（スキーマ＋バリデータ単体で必要十分）。

## 非ゴール / 制約
- 汎用 Catalog/Resolver/Workflow/Agent の Schema は作らない（MVP 範囲外。実装方針 §7.1 末尾・§15）。
- API ルートやモデル永続化は実装しない（EXB-03 で Run/Action API を最小実装）。
- 既存リソース（VCN develop / インスタンス dev / バケット）は参照のみ。
- spec-driven: スキーマの設計判断で迷う点は `docs/decisions/` に ADR 案を残す。

## 依存
EXB-00（方針 ADR・dev ベースライン）。
