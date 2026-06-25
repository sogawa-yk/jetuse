#!/usr/bin/env bash
# SessionStart hook: loop モードのときだけ run を採番し manifest を作る。
# loop モードの判定 = 環境変数 LOOP_TASK がセットされている（例: LOOP_TASK=auth-refactor claude）。
# 通常の開発セッションでは LOOP_TASK 未設定 → 完全な no-op（runs/ を汚さない）。
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"

if [ -z "${LOOP_TASK:-}" ]; then
  rm -f .current_run_id   # 前回 loop セッションの残骸を掃除（通常セッションで履歴記録を誤発火させない）
  exit 0
fi

RUN_ID="$(date +%Y-%m-%dT%H%M)_${LOOP_TASK}"
echo "$RUN_ID" > .current_run_id
DIR="runs/${RUN_ID}"
mkdir -p "$DIR/turns" "$DIR/diffs" "$DIR/reviews"

printf '%s\n' "${GOAL:-<未登録: /goal で完了条件を登録すること>}" > "$DIR/goal.txt"

CLAUDE_VER="$(claude --version 2>/dev/null | awk '{print $1}' || echo unknown)"
CODEX_VER="$(codex --version 2>/dev/null | awk '{print $2}' || echo unknown)"

cat > "$DIR/manifest.json" <<JSON
{
  "run_id": "${RUN_ID}",
  "task_file": "tasks/${LOOP_TASK}.md",
  "goal_condition_path": "runs/${RUN_ID}/goal.txt",
  "loop_config_snapshot": { "stage": "report-only" },
  "tool_versions": { "claude_code": "${CLAUDE_VER}", "codex": "${CODEX_VER}" },
  "started_at": "$(date -Iseconds)",
  "ended_at": null,
  "outcome": "in_progress",
  "totals": { "turns": 0, "tokens": 0, "reviews": 0, "review_fail": 0 }
}
JSON

echo "[loop] run 開始: ${RUN_ID}" >&2
