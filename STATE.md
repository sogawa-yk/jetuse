# STATE（単一の真実源 / 会話の外）

run_id: 2026-06-25T1545_SBA-01
stage: report-only
review_verdict: PASS        # PASS | FAIL | N/A（review-1: blocker 0 / major 3）
last_review_ref: runs/2026-06-25T1545_SBA-01/reviews/review-1.json
updated_at: 2026-06-25T15:45+00:00

## 現在のタスク
SBA-01 サンプル業務アプリの構造定義（scaffold テンプレモデル）。area: api(＋docs)。

## 未完タスク
- [x] manifest を kind: sample-app に拡張（screens / データシード / AI組込スロット）
- [x] scaffold 取込ロジック（sample-app 定義をインスタンスへ展開）を実装
- [x] 合成バリデーションの土台（必要ケイパビリティ／権限スコープの宣言）
- [x] sample-app 定義スキーマの検証＋取込の単体テストが全件パス（275 passed / ruff clean）
- [ ] 実環境 E2E（jetuse-dev / loop ADB / 専用スキーマ JETUSE_SBA01）を複数シナリオ実施・証跡記録
- [ ] 証跡込み Codex レビューが PASS

## 完了タスク
- 静的実装＋単体テスト（review-1 = PASS, blocker 0）

## 直近のレビュー指摘（要約）
review-1（PASS / blocker 0, major 3）:
- major: seed 値が DatasetField.type と未照合 → 修正済（_value_matches_type で型検証）
- major: screens/datasets/aiSlots/fields・seed 総数の件数上限なし → 修正済（MAX_* 上限導入）
- major: 実環境 E2E 証跡が空 → 完了ゲートで E2E 実施中（runs/<run-id>/e2e/）

## 変更ファイル（未コミット）
- packages/api/jetuse_core/plugins/manifest.py（kind に sample-app 追加）
- packages/api/jetuse_core/plugins/sample_app.py（新規: 定義スキーマ＋合成バリデーション土台）
- packages/api/jetuse_core/plugins/scaffold.py（新規: scaffold 取込ロジック）
- packages/api/jetuse_core/plugins/__init__.py（re-export）
- packages/api/jetuse_core/migrations/016_sample_app_instances.sql（新規）
- packages/api/tests/test_sample_app.py / test_scaffold.py（新規）
- packages/api/tests/test_plugin_manifest.py（kind enum 期待値更新）
