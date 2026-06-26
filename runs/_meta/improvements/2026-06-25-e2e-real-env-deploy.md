# 改善: 完了ゲートにデプロイ＋実環境 E2E を導入（jetuse-dev）

- 日付: 2026-06-25
- 起票: 人間指示（/loop-doctor）「Codex レビュー時に jetuse-dev 実環境での E2E（複数シナリオ）を含める。
  タスク依存でベストエフォート可。Codex 依頼前に Claude がデプロイしておく」
- 承認: 人間（カデンス=完了ゲートで1回 / dev環境=固定 loop 環境を再利用 を選択）

## 症状
ループの「完了」判定が静的 Codex レビュー＋fake 単体テストに閉じ、実環境固有の欠陥を検出できない。

## 履歴上の証跡（対象 run）
runs/2026-06-25T1230_PLG-02/reviews/ で Codex が実環境検証の欠如を 5 連続指摘:
- review-1(minor)/review-2(major)/review-3(minor)/review-5(minor): 実 DDL を fake で no-op、Oracle 構文/制約/
  auto-commit 後の部分失敗を検出不能。
- review-4(**blocker**): 実 DB の CLOB(LOB) 挙動。fake では出ず実環境なら一発で出る欠陥。

## 根本原因
完了述語に「実環境 E2E 検証ステージ」が存在しない。Codex は read-only で実行不可のため、
Claude が deploy+E2E を実施して証跡を残し、Codex はその証跡＋diff を評価する形が必要。

## 変更（対象ファイル）
1. `loop-config.yml`: `areas.{web,api}` に `deploy_cmd`/`e2e_cmd` 追加。新規 `e2e:` ブロック
   （cadence=completion_gate / compartment=jetuse-dev / dev_env=loop / min_scenarios=2 / best_effort=true）。
   `goal_template` に述語(4)＝実環境 E2E 実施・証跡記録・証跡込み Codex PASS を追加。
2. `tasks/_template.md`: 「## E2E シナリオ（実環境 / jetuse-dev・複数）」節を新設。
3. `.claude/skills/loop-protocol/SKILL.md`: 「完了ゲート：デプロイ＋実環境 E2E（毎イテレーションでなく1回）」
   ステージを追加（deploy→複数シナリオ E2E→証跡 runs/<id>/e2e/→証跡込みレビュー、SKIPPED.md 必須）。
4. `.claude/skills/codex-review/SKILL.md`: 実環境 E2E 証跡の添付と評価観点を追記。
5. `.claude/skills/codex-review/scripts/run_codex_review.sh`: Codex 入力に runs/<id>/e2e/ 証跡を添付
   （`review-<n>.payload.txt` に保存）。INSTRUCTIONS に E2E 評価を追加。
6. `.claude/skills/codex-review/scripts/review-schema.json`: 任意の `e2e` セクション（attached/
   scenarios_reviewed/adequacy/notes）を追加（非 E2E レビューは後方互換）。

## 設計判断・副作用
- カデンス=完了ゲートで1回（毎イテレーションだと build/push＋terraform apply＋ADB起床を毎回繰り返し高コスト）。
- jetuse-dev の固定 loop 環境を再利用（リソースを増やさない。作り直しは Terraform 破棄→再作成。
  CLAUDE.md「環境・認証の扱い」/ memory jetuse-dev-terraform-resources-ok と整合）。
- 人間ゲートは維持: IAM/テナンシ変更・既存リソース変更・コミット/PR/push。

## 検証
- `bash -n run_codex_review.sh` / `review-schema.json` JSON / `loop-config.yml` YAML すべて構文 OK。
- 効き目は次 run（PLG-03 等、deployable タスク）の runs/<id>/e2e/ と review の e2e セクションで判断する。
