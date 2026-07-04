#!/usr/bin/env bash
# Stop hook: ターン終了（応答完了）ごとに差分とメタを runs/<run-id>/ に追記する。
# loop モード外（LOOP_TASK 未設定 or .current_run_id 無し）では no-op。
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"

[ -z "${LOOP_TASK:-}" ] && exit 0
RUN_ID="$(cat .current_run_id 2>/dev/null || true)"
[ -z "$RUN_ID" ] && exit 0
DIR="runs/${RUN_ID}"
[ -d "$DIR" ] || exit 0

N="$(( $(find "$DIR/turns" -maxdepth 1 -name 'turn-*.json' 2>/dev/null | wc -l) + 1 ))"
git diff HEAD > "${DIR}/diffs/turn-${N}.diff" 2>/dev/null || true

FILES_JSON="$(git diff HEAD --name-only 2>/dev/null \
  | python3 -c 'import sys,json; print(json.dumps([l.strip() for l in sys.stdin if l.strip()], ensure_ascii=False))' \
  || echo '[]')"

# 直近レビューの参照（あれば）
LAST_REVIEW="$(ls "$DIR"/reviews/review-*.json 2>/dev/null | sort | tail -1 || true)"
REVIEW_REF="null"
[ -n "$LAST_REVIEW" ] && REVIEW_REF="\"reviews/$(basename "$LAST_REVIEW")\""

cat > "${DIR}/turns/turn-${N}.json" <<JSON
{
  "turn": ${N},
  "timestamp": "$(date -Iseconds)",
  "action_summary": "(loop-protocol 側で追記可)",
  "files_changed": ${FILES_JSON},
  "diff_path": "diffs/turn-${N}.diff",
  "review_ref": ${REVIEW_REF},
  "goal_checker": { "verdict": "(/goal 判定モデルが記録)", "reason": "" }
}
JSON
