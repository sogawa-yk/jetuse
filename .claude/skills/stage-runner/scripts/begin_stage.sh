#!/usr/bin/env bash
# stage-runner: ステージ専用の統合ブランチと作業 worktree を用意する。
#
# 目的: PASS したタスクを「ステージ専用のローカルブランチ」へ自動 commit+merge して波を繋ぎ、
#       ステージ完了で1回だけ人間に報告する。リモート push / base への PR / apply は一切しない。
#       自動統合はこの隔離ブランチ限定なので、人間チェック前に base やリモートは汚れない。
#
# 使い方: [BASE_BRANCH=feat/loop-engineering] [LOOP_WORKTREE_ROOT=/path] \
#         .claude/skills/stage-runner/scripts/begin_stage.sh <stage-id>
#   例: begin_stage.sh stage-2
# 出力: 最終行(stdout)に統合 worktree のパスを返す（呼び出し側=start-stage.sh が使う）。
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

STAGE="${1:?usage: begin_stage.sh <stage-id>}"
BASE="${BASE_BRANCH:-feat/loop-engineering}"
BR="feat/${STAGE}"
WT_ROOT="${LOOP_WORKTREE_ROOT:-$(cd "$ROOT/.." && pwd)/$(basename "$ROOT")-loops}"
WT="$(realpath -m "${WT_ROOT}/_${STAGE}")"

mkdir -p "$WT_ROOT"

# 統合ブランチ作成/再利用（base から分岐）。
if git show-ref --verify --quiet "refs/heads/${BR}"; then
  echo "[stage] 既存統合ブランチを再利用: $BR" >&2
elif git show-ref --verify --quiet "refs/heads/${BASE}"; then
  git branch "$BR" "$BASE" >&2
  echo "[stage] 統合ブランチ作成: $BR (base=$BASE)" >&2
else
  echo "[stage] ERROR: base ブランチ '$BASE' が見つからない。BASE_BRANCH を指定してください。" >&2
  exit 1
fi

# 統合 worktree 作成/再利用。
if git worktree list --porcelain | grep -qx "worktree ${WT}"; then
  echo "[stage] 既存 worktree を再利用: $WT" >&2
elif [ -e "$WT" ]; then
  echo "[stage] ERROR: $WT が worktree でない実体として存在します。退避してください。" >&2
  exit 1
else
  git worktree add "$WT" "$BR" >&2
fi

# ステージ run ディレクトリ（報告・断面の置き場）。
SDIR="runs/_stages/${STAGE}"
mkdir -p "$SDIR"
if [ ! -f "$SDIR/started_at.txt" ]; then
  date -Iseconds > "$SDIR/started_at.txt"
fi

echo "[stage] integration_branch=$BR worktree=$WT base=$BASE report_dir=$SDIR" >&2
echo "$WT"
