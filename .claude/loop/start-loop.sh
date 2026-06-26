#!/usr/bin/env bash
# ループ起動ランチャ: タスクごとに独立した git worktree を用意し、その中で claude を起動する。
#
# 目的: 共有作業ツリーで複数の loop セッションを同時に回すと、ブランチ・インデックス・作業ツリーを
#       取り合って互いの変更を壊す（実害事例あり）。タスク=1 worktree に分離して物理的に防ぐ。
#
# 使い方:
#   [GOAL="完了条件"] [CODEX_MODEL=...] [BASE_BRANCH=feat/loop-engineering] \
#   [LOOP_WORKTREE_ROOT=/path] [LOOP_SKIP_BOOTSTRAP=1] .claude/loop/start-loop.sh <task-id>
#
# 既定の worktree 配置: <repo>/../<repo名>-loops/<task-id>（リポジトリ外の兄弟ディレクトリ）。
# 依存タスクを連鎖させたい場合は BASE_BRANCH=feat/<dep> を渡す（依存先ブランチから派生）。
# 後始末は .claude/loop/end-loop.sh <task-id>。
set -euo pipefail

TASK="${1:?usage: start-loop.sh <task-id>}"
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

BASE="${BASE_BRANCH:-feat/loop-engineering}"
BR="feat/${TASK}"
WT_ROOT="${LOOP_WORKTREE_ROOT:-$(cd "$ROOT/.." && pwd)/$(basename "$ROOT")-loops}"
WT="$(realpath -m "${WT_ROOT}/${TASK}")"

mkdir -p "$WT_ROOT"

# 既存 worktree を再利用、無ければ作成。
if git worktree list --porcelain | grep -qx "worktree ${WT}"; then
  echo "[loop] 既存 worktree を再利用: $WT" >&2
elif [ -e "$WT" ]; then
  echo "[loop] ERROR: $WT が worktree でない実体として存在します。退避してください。" >&2
  exit 1
elif git show-ref --verify --quiet "refs/heads/${BR}"; then
  git worktree add "$WT" "$BR" >&2
elif git show-ref --verify --quiet "refs/heads/${BASE}"; then
  git worktree add -b "$BR" "$WT" "$BASE" >&2
else
  echo "[loop] ERROR: base ブランチ '$BASE' が見つからない。BASE_BRANCH を指定してください。" >&2
  exit 1
fi
echo "[loop] worktree=$WT branch=$BR base=$BASE" >&2

# 環境ブートストラップ（任意・冪等）。失敗してもセッションは続行する。
if [ "${LOOP_SKIP_BOOTSTRAP:-0}" != "1" ]; then
  "$ROOT/.claude/loop/bootstrap-env.sh" "$WT" "$TASK" \
    || echo "[loop] 環境ブートストラップをスキップ/失敗（手動セットアップしてください）" >&2
fi

cd "$WT"
export LOOP_TASK="$TASK"

# 起動モード分岐:
# - LOOP_AUTONOMOUS=1（オーケストレータが無人ペインで回す並列モード）:
#   権限プロンプトで止まらず自走する（bypassPermissions）。ただし「ループの価値＝人間ゲートを
#   飛ばさない」ため、コミット/PR/push/merge/apply/destroy は --disallowedTools で権限層からも遮断する。
#   完了条件は呼び出し側が GOAL env で登録済み（session_start.sh が goal.txt に記録）。
# - 未設定（人間が付く逐次/worktree 起動）: 従来どおり対話モード。GOAL env を渡せば goal.txt に記録される。
if [ "${LOOP_AUTONOMOUS:-0}" = "1" ]; then
  echo "[loop] 自律モードで起動（bypassPermissions＋ハードゲート deny / LOOP_TASK=$TASK）。" >&2
  exec claude --permission-mode bypassPermissions \
    --disallowedTools \
      "Bash(git commit:*)" "Bash(git push:*)" "Bash(git merge:*)" \
      "Bash(gh pr create:*)" "Bash(gh pr merge:*)" \
      "Bash(terraform apply:*)" "Bash(terraform destroy:*)"
else
  echo "[loop] worktree で起動します（cd $WT / LOOP_TASK=$TASK）。完了条件は GOAL env で登録（goal.txt）。" >&2
  exec claude
fi
