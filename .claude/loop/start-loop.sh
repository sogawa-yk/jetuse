#!/usr/bin/env bash
# ループ起動ランチャ: タスクごとに独立した git worktree を用意し、その中で claude を起動する。
#
# 目的: 共有作業ツリーで複数の loop セッションを同時に回すと、ブランチ・インデックス・作業ツリーを
#       取り合って互いの変更を壊す（実害事例あり）。タスク=1 worktree に分離して物理的に防ぐ。
#
# 使い方:
#   [GOAL="完了条件"] [CODEX_MODEL=...] [BASE_BRANCH=internal-dev] \
#   [LOOP_WORKTREE_ROOT=/path] [LOOP_SKIP_BOOTSTRAP=1] .claude/loop/start-loop.sh <task-id>
#
# 既定の worktree 配置: <repo>/../<repo名>-loops/<task-id>（リポジトリ外の兄弟ディレクトリ）。
# 依存タスクを連鎖させたい場合は BASE_BRANCH=feat/<dep> を渡す（依存先ブランチから派生）。
# 後始末は .claude/loop/end-loop.sh <task-id>。
set -euo pipefail

TASK="${1:?usage: start-loop.sh <task-id>}"
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

# 既定の派生元は loop-config.yml の worktree.base_branch（＝public-dev）に合わせる。
# 共有物は Public 起点で作らないと main へ届かない(ADR-0028)。内部固有は BASE_BRANCH=internal-dev。
# 依存連鎖は BASE_BRANCH=feat/<dep> で上書きする。
BASE="${BASE_BRANCH:-public-dev}"

# 既存ブランチ/worktree の再利用は、**旧既定(dev)から切った枝をそのまま使い続ける**経路になる
# （review-6 F003）。base の先端を含んでいなければ起点がずれている可能性を告げる。
# 失敗にはしない —— 依存連鎖(BASE_BRANCH=feat/<dep>)や単に古いだけの枝を止めてしまうため。
warn_if_base_not_ancestor() {  # $1=branch $2=base
  local _b="$1" _base="$2" _r
  for _r in "refs/heads/${_base}" "refs/remotes/origin/${_base}"; do
    git show-ref --verify --quiet "$_r" || continue
    if ! git merge-base --is-ancestor "$_r" "$_b" 2>/dev/null; then
      echo "[loop] WARN: 既存の $_b は $_base の先端を含んでいません（起点がずれている可能性）。" >&2
      echo "[loop]       意図した起点か確認し、必要なら git rebase $_base してください。" >&2
    fi
    return 0
  done
}

BR="feat/${TASK}"
WT_ROOT="${LOOP_WORKTREE_ROOT:-$(cd "$ROOT/.." && pwd)/$(basename "$ROOT")-loops}"

mkdir -p "$WT_ROOT"
# BSD realpath（macOS）には未作成パスを正規化する -m が無い。ディレクトリ部だけ実体解決する。
WT="$(cd "$WT_ROOT" && pwd)/${TASK}"

# 既存 worktree を再利用、無ければ作成。
if git worktree list --porcelain | grep -qx "worktree ${WT}"; then
  echo "[loop] 既存 worktree を再利用: $WT" >&2
  warn_if_base_not_ancestor "$BR" "$BASE"
elif [ -e "$WT" ]; then
  echo "[loop] ERROR: $WT が worktree でない実体として存在します。退避してください。" >&2
  exit 1
elif git show-ref --verify --quiet "refs/heads/${BR}"; then
  warn_if_base_not_ancestor "$BR" "$BASE"
  git worktree add "$WT" "$BR" >&2
else
  # base はローカルブランチが無ければ origin/<base> から分岐する（ローカルに public-dev を持たない
  # 運用が普通なので、ローカル ref だけを見ると起動できない）。
  if git show-ref --verify --quiet "refs/heads/${BASE}"; then
    BASE_REF="$BASE"
  elif git show-ref --verify --quiet "refs/remotes/origin/${BASE}"; then
    BASE_REF="origin/${BASE}"
    echo "[loop] ローカルに $BASE が無いため origin/$BASE から分岐する" >&2
  else
    echo "[loop] ERROR: base '$BASE' がローカルにも origin にも無い。git fetch するか BASE_BRANCH を指定してください。" >&2
    exit 1
  fi
  git worktree add -b "$BR" "$WT" "$BASE_REF" >&2
fi
echo "[loop] worktree=$WT branch=$BR base=$BASE" >&2

# 報告パイプ（docs/guides/report-pipe.md）の出力先解決に使う .obsidian-dir は gitignore 済みで、
# git worktree add では worktree に来ない。親チェックアウトにあればコピーする（無人ループが
# 出力先の確認プロンプトで止まるのを防ぐ）。無ければ報告は fallback（Artifact 提示）で動く。
if [ -f "$ROOT/.obsidian-dir" ] && [ ! -f "$WT/.obsidian-dir" ]; then
  cp "$ROOT/.obsidian-dir" "$WT/.obsidian-dir"
  echo "[loop] .obsidian-dir を worktree へ複製（報告パイプ用）" >&2
fi

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
  echo "[loop] 自律モードで起動（bypassPermissions＋ハードゲート deny / LOOP_TASK=${TASK}）。" >&2
  exec claude --permission-mode bypassPermissions \
    --disallowedTools \
      "Bash(git commit:*)" "Bash(git push:*)" "Bash(git merge:*)" \
      "Bash(gh pr create:*)" "Bash(gh pr merge:*)" \
      "Bash(terraform apply:*)" "Bash(terraform destroy:*)"
else
  echo "[loop] worktree で起動します（cd $WT / LOOP_TASK=${TASK}）。完了条件は GOAL env で登録（goal.txt）。" >&2
  exec claude
fi
