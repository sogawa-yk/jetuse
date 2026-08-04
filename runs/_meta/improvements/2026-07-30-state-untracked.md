# 2026-07-30 STATE.md を git 追跡外にした（loop-doctor R1）

- 対象 run: `runs/2026-07-28T1848_SPIKE-M1/` / `runs/2026-07-29T1605_RP-01/` /
  `runs/2026-07-30T0025_RAGM-02/`（いずれもマージ時に STATE.md が衝突）
- 対象ファイル: `.gitignore` / `CLAUDE.md` / `.claude/skills/loop-protocol/SKILL.md`
- 承認: 2026-07-30 ユーザー承認（loop-doctor の推薦 R1）

## 症状

並列ループを main へ入れるたび `STATE.md` が必ずコンフリクトする。どちらを採るかを毎回
人間が判断させられ、main の `STATE.md` は「最後にマージしたタスクの状態」しか示さない。

## 証跡

- `git log --all -- STATE.md`: SPIKE-M1 / RP-01 / RAGM-02 が同一パスを奪い合い、
  **3 回連続で衝突解消コミット**が発生している。
- `.claude/skills/stage-runner/SKILL.md:49`: **上位ループは元から
  「STATE.md / runs / dist / .current_run_id は除外」してコミットしている**。
  タスク単位でコミットする経路だけがこの規律から外れていた＝設計意図としては非追跡が正。

## 根本原因

`STATE.md` は「作業中のローカル状態」なのに、git 追跡下のリポジトリ直下の単一ファイルに
なっている。タスクごとの worktree が自分の状態で全面上書きするため、2 本目以降の
マージで衝突するのは構造上の必然。

## 変更

1. `.gitignore` に `STATE.md` を追加（＋ `git rm --cached STATE.md`）
2. `CLAUDE.md`「単一の真実源」を「現在状態＝`STATE.md`（**ローカル・git 追跡外**）」へ明確化
3. `loop-protocol/SKILL.md` に節を追加。**停止直前に `runs/<run-id>/STATE.md` へ写しを残す**

## 副作用

PR の差分から `STATE.md` が消えるため、レビュー時にタスク状態が見えなくなる。
写しを `runs/<run-id>/` に残すことで代替する（run 配下なのでタスク間で衝突しない）。

## 検証

次に並列ループを 2 本以上 main へ入れたときに `STATE.md` が衝突しないことで判断する。

## 未適用の推薦（同じ診断で提示・今回は見送り）

- **R2**: 自律モードの deny が `Bash(git merge:*)` を含み、マージタスクを無人ループで
  開始できない（SYNC-01 の turn-1 が `files_changed: []` で空転）。
- **R3**: `runs/*/turns/*.json` の `action_summary` が **26/26 でプレースホルダのまま**、
  `goal_checker.reason` が **26/26 で空**。履歴が診断に使えていない。
