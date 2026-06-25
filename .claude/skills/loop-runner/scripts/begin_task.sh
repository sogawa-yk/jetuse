#!/usr/bin/env bash
# loop-runner: 同一セッション内で複数タスクを順に回すため、タスクごとに run-id を切り直す。
# session_start.sh のロジックをタスク単位で再実行する（Stop hook は .current_run_id を毎ターン読むので、
# ここで差し替えれば以降の履歴は新タスクの run に記録される）。
# 使い方: GOAL="<完了条件>" .claude/skills/loop-runner/scripts/begin_task.sh <task-id>
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

TASK="${1:?usage: begin_task.sh <task-id>}"

# タスク開始時にブランチを自動で切る（失敗時は伝播させ、runner を停止させる）。
"$ROOT/.claude/hooks/ensure_task_branch.sh" "$TASK"

# 直前タスクの run を completed 扱いに（あれば）
PREV="$(cat .current_run_id 2>/dev/null || true)"
if [ -n "$PREV" ] && [ -f "runs/$PREV/manifest.json" ]; then
  tmp="$(mktemp)"
  sed 's/"outcome": "in_progress"/"outcome": "completed"/' "runs/$PREV/manifest.json" > "$tmp" && mv "$tmp" "runs/$PREV/manifest.json"
fi

RUN_ID="$(date +%Y-%m-%dT%H%M)_${TASK}"
echo "$RUN_ID" > .current_run_id
DIR="runs/${RUN_ID}"
mkdir -p "$DIR/turns" "$DIR/diffs" "$DIR/reviews"

printf '%s\n' "${GOAL:-<未登録: /goal で完了条件を登録すること>}" > "$DIR/goal.txt"

CLAUDE_VER="$(claude --version 2>/dev/null | awk '{print $1}' || echo unknown)"
CODEX_VER="$(codex --version 2>/dev/null | awk '{print $2}' || echo unknown)"

cat > "$DIR/manifest.json" <<JSON
{
  "run_id": "${RUN_ID}",
  "task_file": "tasks/${TASK}.md",
  "goal_condition_path": "runs/${RUN_ID}/goal.txt",
  "loop_config_snapshot": { "stage": "report-only", "driver": "loop-runner" },
  "tool_versions": { "claude_code": "${CLAUDE_VER}", "codex": "${CODEX_VER}" },
  "started_at": "$(date -Iseconds)",
  "ended_at": null,
  "outcome": "in_progress",
  "totals": { "turns": 0, "tokens": 0, "reviews": 0, "review_fail": 0 }
}
JSON

echo "[loop-runner] task 開始: ${TASK} → ${RUN_ID}" >&2
