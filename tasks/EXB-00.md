# タスク: EXB-00 ベースライン確定と方針 ADR

## ゴール
`dev` を Experience Builder（v2）の起点として確定する。main 由来の既存テストが `dev` で回帰なく通ることを
ベースラインとして固定し、方針 ADR（ADR-0022）をドラフトする。新規バックエンド/API は作らない。

## 対象 area
both（主にドキュメント＋既存テストの回帰確認。コード変更は最小）

## 受け入れ条件（検証可能な述語で書く）
- [ ] `dev` 上で api の既存テストが全件パスする（`.venv/bin/pytest packages/api/tests`、回帰なし）。
- [ ] `dev` 上で web がビルドできる（`npm --prefix packages/web run build` 成功）。
- [ ] `docs/decisions/ADR-0022-experience-builder.md` をドラフトする。最低限、次を明記する:
  - Experience Builder を **main 派生の `dev`** を根として進める（docs の `next/experience-builder` を dev に読み替え）。
  - **MVP は引用付き RAG（`answer.with-citations@1`）の縦切り1本**に限定する。
  - 汎用 Catalog/Resolver/Workflow Runtime/Hosted Runtime/Marketplace は **Gate 成立まで作らない**。
  - 既存 `main` 由来 AI 実装は書き直さず **Provider Adapter から再利用**する（実装方針 §3.5 / §12.2）。
  - ステージ統合は `feat/stage-<N>`、自動統合は隔離ブランチ限定（push/PR/apply は人間ゲート）。
- [ ] `README.md`（または `docs/architecture/` の索引）から v2 方針ドキュメント群へのリンクを張る。
- [ ] ruff / 型チェックがクリーン（コード変更がある場合）。

## E2E シナリオ（実環境 / jetuse-dev・複数）
本タスクは契約/ベースライン確定であり、OCI 実リソース面の新規挙動を持たない。
- [ ] シナリオ1（ベースライン回帰）: `dev` で api テスト・web build を実行し、結果を `runs/<run-id>/e2e/` に記録
  （main 由来テストが緑であること＝回帰なしの証跡）。
- [ ] 実 OCI デプロイを伴う E2E は該当なし。`runs/<run-id>/e2e/SKIPPED.md` に「契約/ドキュメント＋既存テスト回帰
  確認のみで、新規の実環境到達面が無い」旨を明記する（無言スキップ禁止）。

## 非ゴール / 制約
- 新しい API（`/api/v1`）・モデル・Provider をこのタスクでは実装しない（EXB-01 以降）。
- 既存 `main` の AI 実装・ルートを書き換えない（参照・再利用方針の明文化に留める）。
- spec-driven: 仕様にない実装判断は ADR 案として残し、人間レビューを要求する（ADR-0022 がそれ）。
- ADR-0022 の**承認は人間ゲート**（stage-runner はドラフトまで。承認はステージ報告で仰ぐ）。
