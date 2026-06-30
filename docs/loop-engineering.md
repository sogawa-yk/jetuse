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
| 並行分離 | `.claude/loop/start-loop.sh`（worktree 起動）/ `end-loop.sh`（撤去）/ `bootstrap-env.sh`（隔離環境） |

## 回し方

1. **タスクを書く**: `tasks/<task>.md`（`tasks/_template.md` を複製）。受け入れ条件は検証可能な述語で。
2. **loop モードで起動**（推奨＝worktree 分離）:
   ```bash
   GOAL="$(完了条件文字列)" CODEX_MODEL=<任意> .claude/loop/start-loop.sh <task>
   ```
   - タスクごとに独立した **git worktree**（既定 `../<repo名>-loops/<task>`）を作り、その中で
     `LOOP_TASK=<task> claude` を起動する。ブランチ・インデックス・作業ツリーを共有しないので、
     **複数の loop を同時に回しても互いの変更を壊さない**（共有チェックアウトでの衝突実害を受けて導入）。
   - 作成時に隔離環境を用意（api=専用 `.venv`+editable install / web=`node_modules`）。
     `LOOP_SKIP_BOOTSTRAP=1` で無効化。依存連鎖は `BASE_BRANCH=feat/<dep>` で派生元を変える。
   - 後始末は `.claude/loop/end-loop.sh <task>`（マージ/中断後に worktree を撤去。ブランチは保持）。
   - SessionStart hook が `runs/<日時>_<task>/` を採番し `manifest.json` / `goal.txt` を作る
     （manifest に `isolation.worktree` を記録）。

   <details><summary>後方互換: 共有チェックアウトで回す（単一セッション時のみ）</summary>

   ```bash
   LOOP_TASK=<task> GOAL="$(完了条件文字列)" CODEX_MODEL=<任意> claude
   ```
   この場合 SessionStart hook が `feat/<task>` へ自動切替する。**並行起動は厳禁**（ブランチを
   取り合って衝突する）。並行が要るなら必ず `start-loop.sh` を使う。
   </details>
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
3. **auto-commit**: allowlist の安全範囲のみ自動コミット。並行は worktree 分離（`start-loop.sh`・実装済み）。

引き上げはいずれも人間承認（ヒューマンゲート）。

## 人間ゲート（必ず承認が要る操作）

- コミット / PR / push / リリース
- `loop-doctor` による仕組みの編集
- stage の引き上げ
- 破壊的操作・アクセス権変更・認証情報入力（設計上行わない）

## 並行運用と worktree 分離

複数タスクを同時に回す（別ターミナル/別セッション）ときは、**必ず `start-loop.sh` で起動**する。

```
<repo>/                         # 共有チェックアウト（通常開発はここ）
<repo>-loops/<taskA>/           # taskA の worktree（branch feat/<taskA>）
<repo>-loops/<taskB>/           # taskB の worktree（branch feat/<taskB>）
```

- 各 worktree は独立した作業ツリー・インデックスを持ち、`.git` 本体（履歴・refs）だけを共有する。
  → ブランチ切替・`git add`・ファイル編集が互いに干渉しない。
- `runs/<run-id>/` は worktree 内に作られ、タスクのブランチと一緒に履歴として運ばれる。
- `.venv` / `node_modules` は worktree ごとに用意（editable install の隔離のため。`bootstrap-env.sh`）。
- 撤去は `.claude/loop/end-loop.sh <task>`。未コミット変更があると既定で拒否（`--force` で強制）。

> 共有チェックアウトで loop を**並行**起動すると、ブランチ・インデックスを取り合って互いの変更を
> 壊す（実害事例あり）。単一セッションなら共有チェックアウトでも可だが、並行は worktree 必須。

## トラブル時

| 症状 | 渡す先 |
| --- | --- |
| 同じ指摘が再発 / レビューが甘い・過剰 / 終わらない / トークン浪費 | `loop-doctor` |
| 履歴が記録されない / 空の run が出る | `loop-doctor`（hooks の LOOP_TASK ガードを点検） |
| 並行セッションでブランチが入れ替わる / ファイルが消える・上書きされる | `start-loop.sh` で worktree 起動に切替 |

## 注意（loop-impl.md §9）

- maker/checker 二重実行はコスト高。`loop-config.yml` の `budget` と `manifest.totals` で計測。
- 「done は主張であって証明ではない」。最終的に動作確認するのは人間。stage を急がない。
- `codex exec` のフラグ・モデルはバージョン依存。`manifest.json` の `tool_versions` に実バージョンを残す。
