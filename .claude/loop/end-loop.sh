#!/usr/bin/env bash
# ループ後始末: タスクの worktree を安全に撤去する（マージ/中断後）。
#
# 使い方: [LOOP_WORKTREE_ROOT=/path] .claude/loop/end-loop.sh <task-id> [--force]
#   - 既定では未コミット変更があると撤去を拒否する（取りこぼし防止）。--force で強制撤去。
#   - ブランチ feat/<task> は消さない（マージ確認は人間ゲート）。worktree の作業コピーのみ撤去。
set -euo pipefail

TASK="${1:?usage: end-loop.sh <task-id> [--force]}"
FORCE="${2:-}"
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

WT_ROOT="${LOOP_WORKTREE_ROOT:-$(cd "$ROOT/.." && pwd)/$(basename "$ROOT")-loops}"
WT="$(realpath -m "${WT_ROOT}/${TASK}")"

if ! git worktree list --porcelain | grep -qx "worktree ${WT}"; then
  echo "[loop] worktree が見つからない: $WT（既に撤去済み？）" >&2
  exit 0
fi

if [ "$FORCE" = "--force" ]; then
  git worktree remove --force "$WT"
else
  git worktree remove "$WT" \
    || { echo "[loop] 未コミット変更があり撤去を中止。確認後 --force で再実行を。" >&2; exit 3; }
fi
echo "[loop] worktree 撤去: $WT（ブランチ feat/${TASK} は保持）" >&2
