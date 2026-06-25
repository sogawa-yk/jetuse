# ループエンジニアリング 使い方ガイド

Claude Code（実装＝maker）× Codex（レビュー＝checker）× `/goal`（完了採点）の三層ループ。
設計の根拠は `loop-impl.md`、導入時の判断は `docs/decisions/ADR-0012-loop-engineering.md`。

## 構成要素の在りか

| 要素 | 実体 |
| --- | --- |
| ループ憲法 | `CLAUDE.md`「ループエンジニアリング」節 |
| 設定 | `loop-config.yml`（stage / レビュー / 予算 / area別test_cmd / goal_template） |
| 単一の真実源 | `STATE.md`（現在状態）＋ `runs/<run-id>/`（不変の履歴） |
| 毎ターン手順 | `.claude/skills/loop-protocol/` |
| レビュー | `.claude/skills/codex-review/`（+ `scripts/run_codex_review.sh`, `review-schema.json`） |
| 自己改善 | `.claude/skills/loop-doctor/`（+ `references/component-map.md`） |
| 履歴要約 | `.claude/agents/log-summarizer.md` |
| 自動記録 | `.claude/hooks/session_start.sh`（run採番）/ `log_turn.sh`（ターン記録） |

## 回し方

1. **タスクを書く**: `tasks/<task>.md`（`tasks/_template.md` を複製）。受け入れ条件は検証可能な述語で。
2. **loop モードで起動**:
   ```bash
   LOOP_TASK=<task> GOAL="$(完了条件文字列)" CODEX_MODEL=<任意> claude
   ```
   - `LOOP_TASK` が活性化スイッチ。これが無いと hooks は no-op（通常開発と同じ）。
   - SessionStart hook が `runs/<日時>_<task>/` を採番し `manifest.json` / `goal.txt` を作る。
3. **`/goal` を実行**: `loop-config.yml` の `goal_template` を埋めた完了条件を登録する。
   完了条件には必ず「STATE.md の review_verdict が PASS」を含める（停止判定に外部レビューを結ぶ）。
4. **ループが回る**（毎ターン `loop-protocol`）:
   実装 → `codex-review`（差分を Codex に渡し `review-<n>.json` 生成）→ 履歴記録 → STATE 更新。
   FAIL の指摘は次ターンで修正。Claude は `review_verdict` を自分で PASS にできない。
5. **停止**: テスト・lint クリーン かつ review_verdict=PASS で `/goal` が停止。
6. **人間がレビュー** → コミット / PR は**人間承認後**に実行（Stage 1 では自動コミットしない）。
7. **問題があれば** `loop-doctor` に渡す → 仕組みの修正案を提示（承認後のみ編集）。

## 段階導入（loop-config.yml の `stage`）

1. **report-only**（既定）: レビューと履歴記録のみ。コミットしない。まず1週間運用して履歴の十分性を確認。
2. **auto-fix**: レビュー FAIL への同一ツリー内修正まで自動。コミットは手動。
3. **auto-commit**: allowlist の安全範囲のみ自動コミット。並行は worktree 分離。

引き上げはいずれも人間承認（ヒューマンゲート）。

## 人間ゲート（必ず承認が要る操作）

- コミット / PR / push / リリース
- `loop-doctor` による仕組みの編集
- stage の引き上げ
- 破壊的操作・アクセス権変更・認証情報入力（設計上行わない）

## トラブル時

| 症状 | 渡す先 |
| --- | --- |
| 同じ指摘が再発 / レビューが甘い・過剰 / 終わらない / トークン浪費 | `loop-doctor` |
| 履歴が記録されない / 空の run が出る | `loop-doctor`（hooks の LOOP_TASK ガードを点検） |

## 注意（loop-impl.md §9）

- maker/checker 二重実行はコスト高。`loop-config.yml` の `budget` と `manifest.totals` で計測。
- 「done は主張であって証明ではない」。最終的に動作確認するのは人間。stage を急がない。
- `codex exec` のフラグ・モデルはバージョン依存。`manifest.json` の `tool_versions` に実バージョンを残す。
