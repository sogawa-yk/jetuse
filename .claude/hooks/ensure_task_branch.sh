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
# 既定の派生元は loop-config.yml の worktree.base_branch（＝public-dev）に合わせる。
# 共有物は Public 起点で作らないと main へ届かない(ADR-0028)。内部固有は BASE_BRANCH=internal-dev。
BASE="${BASE_BRANCH:-public-dev}"
BR="feat/${TASK}"

cur="$(git branch --show-current 2>/dev/null || true)"
if [ "$cur" = "$BR" ]; then
  echo "[branch] 既に $BR" >&2
  exit 0
fi

# worktree（linked working tree）内ではブランチは固定。共有チェックアウトの切替ロジックは適用しない。
# 並行セッションの衝突は worktree 分離（start-loop.sh）で防ぐ前提。
if [ "$(git rev-parse --git-dir)" != "$(git rev-parse --git-common-dir)" ]; then
  echo "[branch] worktree 内（現在 ${cur}）。期待は ${BR}。ブランチ切替は行わない。" >&2
  echo "[branch] 別タスクの worktree なら start-loop.sh で正しい worktree を起動してください。" >&2
  exit 0
fi

if ! git diff --quiet HEAD 2>/dev/null; then
  echo "[branch] 追跡ファイルに未コミット変更あり → $BR へ切替しない。先にコミット/stash を。" >&2
  exit 3
fi

if git show-ref --verify --quiet "refs/heads/${BR}"; then
  git checkout "$BR" >&2
  # 既存ブランチの再利用は、**旧既定(dev)から切ったブランチをそのまま使い続ける**経路になる
  # （review-5 F001）。base の先端を含んでいなければ起点がずれている可能性を告げる。
  # 失敗にはしない —— 依存連鎖(BASE_BRANCH=feat/<dep>)や単に古いだけの枝を止めてしまうため。
  for _r in "refs/heads/${BASE}" "refs/remotes/origin/${BASE}"; do
    git show-ref --verify --quiet "$_r" || continue
    if ! git merge-base --is-ancestor "$_r" "$BR" 2>/dev/null; then
      echo "[branch] WARN: 既存の $BR は $BASE の先端を含んでいません（起点がずれている可能性）。" >&2
      echo "[branch]       意図した起点か確認し、必要なら git rebase $BASE してください。" >&2
    fi
    break
  done
elif git show-ref --verify --quiet "refs/heads/${BASE}"; then
  git checkout -b "$BR" "$BASE" >&2
elif git show-ref --verify --quiet "refs/remotes/origin/${BASE}"; then
  # ローカルに base が無い運用が普通なので、リモート追跡ブランチから分岐する。
  git checkout -b "$BR" "origin/${BASE}" >&2
else
  # **現在地から作らない。** 以前はフォールバックしていたが、base が無いまま黙って
  # 現在の HEAD から分岐すると、Internal 系ブランチ上で共有物の作業を始めても正常終了し、
  # 起点の保証が静かに破れる（review-4 F001）。start-loop.sh と同じくエラーで止める。
  echo "[branch] ERROR: base '$BASE' がローカルにも origin にも無い。" >&2
  echo "[branch]        git fetch するか BASE_BRANCH を指定してください（既定は public-dev）。" >&2
  exit 1
fi
echo "[branch] $BR に切替（base=${BASE}）" >&2
