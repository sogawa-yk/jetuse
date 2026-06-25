#!/usr/bin/env bash
# タスク開始時にブランチ feat/<task> を用意する（人間確認不要）。
# - 既に feat/<task> なら何もしない
# - worktree 内（start-loop.sh 起動）では起動側がブランチを確定済み → 切替しない（共有汚染ゼロ）
# - 共有チェックアウトでは: 既存ブランチなら checkout、無ければ base から作成
# - 追跡ファイルに未コミット変更があれば中断（前タスクの変更持ち越し事故を防ぐ）。untracked(runs/ 等)は無視。
# 依存タスクは、依存先が base にマージ済みであること。連鎖したい場合は BASE_BRANCH=feat/<dep> を渡す。
#
# 推奨は start-loop.sh による worktree 起動。本フックは共有チェックアウト運用の後方互換パス。
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

TASK="${1:?usage: ensure_task_branch.sh <task-id>}"
BASE="${BASE_BRANCH:-feat/loop-engineering}"
BR="feat/${TASK}"

cur="$(git branch --show-current 2>/dev/null || true)"
if [ "$cur" = "$BR" ]; then
  echo "[branch] 既に $BR" >&2
  exit 0
fi

# worktree（linked working tree）内ではブランチは固定。共有チェックアウトの切替ロジックは適用しない。
# 並行セッションの衝突は worktree 分離（start-loop.sh）で防ぐ前提。
if [ "$(git rev-parse --git-dir)" != "$(git rev-parse --git-common-dir)" ]; then
  echo "[branch] worktree 内（現在 $cur）。期待は $BR。ブランチ切替は行わない。" >&2
  echo "[branch] 別タスクの worktree なら start-loop.sh で正しい worktree を起動してください。" >&2
  exit 0
fi

if ! git diff --quiet HEAD 2>/dev/null; then
  echo "[branch] 追跡ファイルに未コミット変更あり → $BR へ切替しない。先にコミット/stash を。" >&2
  exit 3
fi

if git show-ref --verify --quiet "refs/heads/${BR}"; then
  git checkout "$BR" >&2
elif git show-ref --verify --quiet "refs/heads/${BASE}"; then
  git checkout -b "$BR" "$BASE" >&2
else
  echo "[branch] base($BASE) が無いため現在地から $BR を作成" >&2
  git checkout -b "$BR" >&2
fi
echo "[branch] $BR に切替（base=$BASE）" >&2
