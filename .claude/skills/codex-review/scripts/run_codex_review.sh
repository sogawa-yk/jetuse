#!/usr/bin/env bash
# Codex に直近差分をレビューさせ、構造化 JSON と生出力を runs/<run-id>/reviews/ に残す。
# loop-impl.md A-8 を本リポジトリ向けに具体化（codex 0.142 系で検証）。
#   - 差分は stdin で渡す（ARG_MAX 回避）
#   - --output-schema で review-schema.json 準拠の JSON を直接生成（手動抽出を排除）
#   - codex は read-only sandbox（レビューでファイルを書かせない）
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

SCRIPT_DIR=".claude/skills/codex-review/scripts"
SCHEMA="${SCRIPT_DIR}/review-schema.json"

RUN_ID="$(cat .current_run_id 2>/dev/null || true)"
if [ -z "${RUN_ID}" ]; then
  echo "ERROR: .current_run_id が無い。loop モードで起動していない（SessionStart hook 未発火）。" >&2
  exit 2
fi
REV_DIR="runs/${RUN_ID}/reviews"
mkdir -p "$REV_DIR"
N="$(( $(find "$REV_DIR" -maxdepth 1 -name 'review-*.json' 2>/dev/null | wc -l) + 1 ))"

# --- レビュー対象の差分を決める -------------------------------------------
SCOPE="${DIFF_SCOPE:-uncommitted}"
case "$SCOPE" in
  staged)      DIFF="$(git diff --staged)" ;;
  worktree)    DIFF="$(git diff)" ;;
  uncommitted|*) DIFF="$(git diff HEAD)" ;;
esac

INPUT_DIFF="${REV_DIR}/review-${N}.input.diff"
printf '%s\n' "$DIFF" > "$INPUT_DIFF"

if [ -z "${DIFF//[$'\t\r\n ']/}" ]; then
  echo "WARN: 差分が空。レビューをスキップ（review-${N} は生成しない）。" >&2
  echo "VERDICT: N/A (empty diff)"
  exit 0
fi

# --- codex 呼び出し --------------------------------------------------------
INSTRUCTIONS="あなたは厳格なコードレビュアーです。<stdin> に与える git 差分をレビューしてください。
観点: 正確性 / 境界条件 / エラー処理 / 後方互換（公開シグネチャ） / テスト網羅。
本リポジトリ固有の観点: 認証情報・テナンシ/コンパートメント OCID・エンドポイント実値を
コミットしていないか（環境依存値は .env 管理）。既存リソースを参照のみに留めているか。
各指摘には severity(blocker|major|minor) と file・line・issue・suggestion を付けること。
blocker が1件でもあれば verdict は必ず FAIL。blocker が0件なら PASS。
指摘の見逃しより過剰報告を許容するが、minor を blocker に格上げしないこと。
出力は指定の JSON スキーマに厳密に従うこと。"

RAW="${REV_DIR}/review-${N}.raw.txt"
CORE="${REV_DIR}/review-${N}.core.json"
JSON="${REV_DIR}/review-${N}.json"

CODEX_ARGS=(exec "$INSTRUCTIONS"
  --sandbox read-only
  --output-schema "$SCHEMA"
  --output-last-message "$CORE"
  --json)
if [ -n "${CODEX_MODEL:-}" ]; then
  CODEX_ARGS+=(--model "$CODEX_MODEL")
fi

set +e
printf '%s\n' "$DIFF" | codex "${CODEX_ARGS[@]}" >"$RAW" 2>&1
CODEX_RC=$?
set -e

# --- メタデータで包んで review-<n>.json を確定 -----------------------------
RUN_ID="$RUN_ID" N="$N" CORE="$CORE" JSON="$JSON" RAW="$RAW" \
INPUT_DIFF="$INPUT_DIFF" CODEX_RC="$CODEX_RC" MODEL="${CODEX_MODEL:-<codex default>}" \
python3 - <<'PY'
import json, os, datetime
core_path = os.environ["CORE"]
rc = int(os.environ["CODEX_RC"])
n = int(os.environ["N"])
core = {}
err = None
try:
    with open(core_path, encoding="utf-8") as f:
        core = json.load(f)
except Exception as e:
    err = f"core JSON 読み取り失敗: {e}"

if rc != 0 and not core:
    core = {"verdict": "ERROR", "summary": f"codex 異常終了 rc={rc}",
            "severity_counts": {"blocker": 0, "major": 0, "minor": 0}, "findings": []}
elif err:
    core = {"verdict": "ERROR", "summary": err,
            "severity_counts": {"blocker": 0, "major": 0, "minor": 0}, "findings": []}

# blocker>0 なら verdict を FAIL に矯正（採点者の判定を機械的に担保）
sc = core.get("severity_counts", {})
if sc.get("blocker", 0) > 0 and core.get("verdict") == "PASS":
    core["verdict"] = "FAIL"

out = {
    "review_n": n,
    "timestamp": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    "reviewer": "codex",
    "model": os.environ["MODEL"],
    "input_diff_path": os.path.relpath(os.environ["INPUT_DIFF"], f"runs/{os.environ['RUN_ID']}"),
    "raw_output_path": os.path.relpath(os.environ["RAW"], f"runs/{os.environ['RUN_ID']}"),
    "codex_exit_code": rc,
    **core,
}
with open(os.environ["JSON"], "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print(f"VERDICT: {out.get('verdict')}  ->  {os.environ['JSON']}")
PY

# 中間ファイルは残しても良いが、確定後は core を削除して紛れを防ぐ
rm -f "$CORE"
