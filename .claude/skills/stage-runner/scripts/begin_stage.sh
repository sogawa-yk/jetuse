#!/usr/bin/env bash
# stage-runner: ステージ専用の統合ブランチと作業 worktree を用意する。
#
# 目的: PASS したタスクを「ステージ専用のローカルブランチ」へ自動 commit+merge して波を繋ぎ、
#       ステージ完了で1回だけ人間に報告する。リモート push / base への PR / apply は一切しない。
#       自動統合はこの隔離ブランチ限定なので、人間チェック前に base やリモートは汚れない。
#
# 使い方: [BASE_BRANCH=dev] [LOOP_WORKTREE_ROOT=/path] \
#         .claude/skills/stage-runner/scripts/begin_stage.sh <stage-id>
#   例: begin_stage.sh stage-2
# 出力: 最終行(stdout)に統合 worktree のパスを返す（呼び出し側=start-stage.sh が使う）。
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

STAGE="${1:?usage: begin_stage.sh <stage-id>}"
# 既定の派生元(=ステージ統合の根)。Experience Builder 系列は main 派生の dev を根にする。
BASE="${BASE_BRANCH:-dev}"
BR="feat/${STAGE}"
WT_ROOT="${LOOP_WORKTREE_ROOT:-$(cd "$ROOT/.." && pwd)/$(basename "$ROOT")-loops}"

mkdir -p "$WT_ROOT"
# BSD realpath（macOS）には未作成パスを正規化する -m が無い。ディレクトリ部だけ実体解決する。
WT="$(cd "$WT_ROOT" && pwd)/_${STAGE}"

# 統合ブランチ作成/再利用（base から分岐）。
if git show-ref --verify --quiet "refs/heads/${BR}"; then
  echo "[stage] 既存統合ブランチを再利用: $BR" >&2
else
  # base はローカルブランチが無ければ origin/<base> から分岐する（ローカルに dev を持たない運用が普通）。
  if git show-ref --verify --quiet "refs/heads/${BASE}"; then
    BASE_REF="$BASE"
  elif git show-ref --verify --quiet "refs/remotes/origin/${BASE}"; then
    BASE_REF="origin/${BASE}"
  else
    echo "[stage] ERROR: base '$BASE' がローカルにも origin にも無い。git fetch するか BASE_BRANCH を指定してください。" >&2
    exit 1
  fi
  git branch "$BR" "$BASE_REF" >&2
  echo "[stage] 統合ブランチ作成: $BR (base=$BASE_REF)" >&2
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

# 報告パイプ（docs/guides/report-pipe.md）用。.obsidian-dir は gitignore 済みで worktree に来ない。
if [ -f "$ROOT/.obsidian-dir" ] && [ ! -f "$WT/.obsidian-dir" ]; then
  cp "$ROOT/.obsidian-dir" "$WT/.obsidian-dir"
fi

# ステージ run ディレクトリ（報告・断面の置き場）。
SDIR="runs/_stages/${STAGE}"
mkdir -p "$SDIR"
if [ ! -f "$SDIR/started_at.txt" ]; then
  date -Iseconds > "$SDIR/started_at.txt"
fi

echo "[stage] integration_branch=$BR worktree=$WT base=$BASE report_dir=$SDIR" >&2
echo "$WT"
