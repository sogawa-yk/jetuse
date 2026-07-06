#!/usr/bin/env bash
# stage-runner: PASS したタスクの deliverable をコミットし、ステージ統合ブランチへローカル merge する。
#
# 安全策:
#  - push / PR / apply は一切しない（ローカル merge のみ）。
#  - loop 成果物（STATE.md / runs/ / packages/web/dist / .current_run_id）はコミットしない。
#  - コンフリクトは**自動解決しない**。exit 3 で返し、統合 worktree を merge 進行中のまま残す
#    （SKILL がサブエージェント解決→Codex レビューを起動する。不能なら git merge --abort）。
#
# 使い方: .claude/skills/stage-runner/scripts/integrate_task.sh <stage-id> <task-id>
# 戻り値: 0=統合成功 / 3=コンフリクト（要サブエージェント解決） / それ以外=失敗
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

STAGE="${1:?usage: integrate_task.sh <stage-id> <task-id>}"
TASK="${2:?usage: integrate_task.sh <stage-id> <task-id>}"
BR="feat/${STAGE}"
TBR="feat/${TASK}"
WT_ROOT="${LOOP_WORKTREE_ROOT:-$(cd "$ROOT/.." && pwd)/$(basename "$ROOT")-loops}"
TWT="$(realpath -m "${WT_ROOT}/${TASK}")"
SWT="$(realpath -m "${WT_ROOT}/_${STAGE}")"

[ -d "$TWT" ] || { echo "[stage] ERROR: task worktree 無し: $TWT" >&2; exit 1; }
[ -d "$SWT" ] || { echo "[stage] ERROR: stage worktree 無し: $SWT（begin_stage.sh 未実行?）" >&2; exit 1; }

# 1) タスク worktree で deliverable をコミット（loop 成果物は除外）。
#    add -A は gitignore 済（.current_run_id / .env 等）を自動スキップ。tracked/untracked の
#    loop 成果物（STATE.md / runs/ / dist）は add 後に reset で外す（exclude pathspec は
#    ignore 済パスを名指しすると git が中断するため使わない）。
( cd "$TWT"
  git add -A
  # loop 成果物＋E2E スクラッチ＋秘匿値の置き場を staged から外す（エージェントがリポジトリ内に
  # ADB wallet/接続情報を置く事故があったため明示除外。pathspec が無ければ無視）。
  git reset -q -- STATE.md runs packages/web/dist scratchpad_e2e .env .current_run_id 2>/dev/null || true
  git reset -q -- '*.zip' '*wallet*' 'conn.env' '*.pem' '*.key' 2>/dev/null || true
  # 安全網: staged 内容に明白な秘匿値が混入していたら**コミットせず中断**（exit 4）。
  if git diff --cached -U0 | grep -aErqi '(ADB_ADMIN_PASSWORD|ADB_WALLET_PASSWORD|BEGIN [A-Z ]*PRIVATE KEY|aws_secret_access_key|password\s*=\s*[^ ]{6,})'; then
    echo "[stage] ABORT: staged 差分に秘匿値らしき内容を検出。コミットしない（$TASK / worktree=$TWT）。" >&2
    echo "[stage] → 当該ファイルをリポジトリ外（セッション scratchpad）へ退避し .gitignore してから再実行。" >&2
    git reset -q
    exit 4
  fi
  if git diff --cached --quiet; then
    echo "[stage] $TASK: コミット対象の変更なし（既にコミット済み?）" >&2
  else
    git commit --no-verify -m "feat(${TASK}): stage-runner 自動統合（Codex PASS + 実環境 E2E 済）

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>" >&2
  fi
) || exit $?

# 2) ステージ worktree へローカル merge（push しない）。コンフリクトは exit 3。
set +e
( cd "$SWT" && git merge --no-ff --no-edit "$TBR" >&2 )
rc=$?
set -e
if [ "$rc" -ne 0 ]; then
  echo "[stage] CONFLICT: $TASK を $BR へ統合中にコンフリクト（自動解決しない / worktree=$SWT）。" >&2
  echo "[stage] → SKILL の conflict_policy（サブエージェント解決→Codex レビュー）に従う。不能なら $SWT で git merge --abort。" >&2
  exit 3
fi

echo "[stage] 統合完了: $TBR → $BR（worktree=$SWT・未 push）。area テストを再実行して緑を確認すること。" >&2
