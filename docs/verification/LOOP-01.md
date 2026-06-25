# LOOP-01: ループエンジニアリング scaffold 実機検証

日付: 2026-06-25
ブランチ: `feat/loop-engineering`
関連: `loop-impl.md` / ADR-0012 / `docs/loop-engineering.md`

## 目的
loop-impl.md の scaffold を本リポジトリへ導入し、Claude Code（maker）× Codex（checker）の
レビューループが**実環境で end-to-end に回る**ことを確認する。「ドキュメントにそう書いてある」では
なく、実際の codex 呼び出し結果をもって完了とする（実機検証主義）。

## 環境
- Claude Code `2.1.191`（`/goal` は v2.1.139 で追加済）
- codex-cli `0.142.1`（`codex exec --output-schema` / `--sandbox` / `codex exec review` を確認）
- モノレポ: `packages/web`(vitest/eslint) / `packages/api`(pytest/ruff)

## 実施と結果

### 1. 静的検証（PASS）
- `bash -n` 3スクリプト構文 OK。`settings.json` / `review-schema.json` JSON 妥当性 OK。

### 2. 活性化スイッチ（LOOP_TASK ガード）（PASS）
- `LOOP_TASK` 未設定で `session_start.sh` / `log_turn.sh` を実行 → 完全 no-op（run 生成なし、exit 0）。
  通常開発セッションに影響しないことを確認。
- `LOOP_TASK=smoke` で `session_start.sh` 実行 → `runs/<日時>_smoke/` 採番、`manifest.json`
  （`tool_versions` 実値入り）・`goal.txt` 生成。`log_turn.sh` → `turns/turn-1.json` 生成。

### 3. バグ修正（重要）
- `ls <glob> | wc -l` が `set -euo pipefail` 下で「マッチ無し時に ls が非0終了 → pipefail →
  set -e で即終了」する不具合を `log_turn.sh` / `run_codex_review.sh` で検出。
  `find -maxdepth 1 -name` に置換して修正（初回ターン・初回レビューで停止していた）。
- 空差分時のレビューは正しくスキップ（`VERDICT: N/A (empty diff)`、exit 0）。

### 4. Codex レビュー end-to-end（PASS — 実モデル呼び出し）
- 故意にバグを2つ仕込んだ `_smoke_bug.py`（ゼロ除算ガード無し／refresh 失敗時に旧トークン返却）を
  staged し、`run_codex_review.sh` を実行。
- 結果: `runs/<id>/reviews/review-1.json`（スキーマ準拠）を生成。Codex は**両バグを検出**し、
  さらにテスト不足（minor）を指摘:
  - F-001 major: ゼロ除算の境界条件未処理（line 3）
  - F-002 major: refresh 失敗を検知せず旧トークン返却（line 9）
  - F-003 minor: 新規ロジックのテスト欠如
- 監査証跡: `review-1.input.diff`（入力差分）と `review-1.raw.txt`（JSONL 生出力）を対で保存。
- コスト実測（raw の usage）: input 13,948 / cached 2,432 / output 1,030 / reasoning 516 トークン。
- `verdict: PASS` は設計どおり正しい挙動: 完了をブロックするのは `blocker` のみ（`blocker_blocks_completion:
  true`）。今回は blocker 0・major 2・minor 1 のため PASS。major/minor は記録され次ターンで対処対象になるが
  停止条件は塞がない。`blocker>0` のときは verdict を機械的に FAIL へ矯正する実装も確認。

## 結論
scaffold は実環境で end-to-end に機能する（実モデル呼び出しでバグ検出・構造化記録・監査証跡まで）。
**Stage 1 (report-only) で運用開始可**。

## 未検証 / 次アクション（人間ゲート）
- `/goal` での実ループ起動（完了判定モデルの停止挙動）は次の実タスクで確認する。
- CODEX_MODEL 未指定時は codex 既定モデルを使用。レビュー品質に応じて `loop-config.yml` で固定を検討。
- Stage 2/3 への引き上げは人間承認後。
