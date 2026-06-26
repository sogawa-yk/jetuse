#!/usr/bin/env bash
# stage-runner オーケストレータの起動ランチャ（loop-runner の上位）。
#
# タスクエージェント（start-loop.sh）との違い:
#  - ローカル統合のため git commit / git merge は**許可**する（ステージ専用ブランチ限定の自動統合）。
#  - ただし push / gh pr / terraform apply・destroy は**ハードゲートとして権限層で遮断**する
#    （ステージ自走中も越えない。CLAUDE.md「やってはいけないこと」）。
#  - ステージ統合 worktree（feat/<stage>）の中で claude を起動する。
#
# 使い方: [BASE_BRANCH=feat/loop-engineering] .claude/loop/start-stage.sh <stage-id>
#   例: .claude/loop/start-stage.sh stage-2
# 起動後、セッション内で /stage-runner を実行（または「ステージ2を回して」）すると自走する。
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
STAGE="${1:?usage: start-stage.sh <stage-id>}"

# 統合ブランチ＋ worktree を用意し、その worktree パスを得る。
SWT="$("$ROOT/.claude/skills/stage-runner/scripts/begin_stage.sh" "$STAGE" | tail -1)"
[ -d "$SWT" ] || { echo "[stage] ERROR: 統合 worktree を取得できなかった: $SWT" >&2; exit 1; }

cd "$SWT"
export STAGE_RUNNER="$STAGE"
echo "[stage] オーケストレータ起動: stage=$STAGE worktree=$SWT" >&2
echo "[stage] commit/merge は許可・push/PR/apply/destroy は遮断。/stage-runner で自走させてください。" >&2

exec claude --permission-mode bypassPermissions \
  --disallowedTools \
    "Bash(git push:*)" "Bash(gh pr create:*)" "Bash(gh pr merge:*)" \
    "Bash(terraform apply:*)" "Bash(terraform destroy:*)"
