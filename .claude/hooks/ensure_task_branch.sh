#!/usr/bin/env bash
# タスク開始時にブランチ feat/<task> を自動で切る/切替（人間確認不要）。
# - 既に feat/<task> なら何もしない
# - 既存ブランチなら checkout、無ければ base(BASE_BRANCH 既定 feat/loop-engineering)から作成
# - 追跡ファイルに未コミット変更があれば中断（前タスクの変更持ち越し事故を防ぐ）。untracked(runs/ 等)は無視。
# 依存タスクは、依存先が base にマージ済みであること。連鎖したい場合は BASE_BRANCH=feat/<dep> を渡す。
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
