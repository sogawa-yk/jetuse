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
コミットしていないか（環境依存値は .env 管理）。既存リソースを参照のみに留めているか
（jetuse-dev への開発リソース作成は承認済み。IAM/テナンシ変更・既存リソース変更は人間ゲート）。
<stdin> の後半に「===== 実環境 E2E 証跡 =====」がある場合、それは Claude が jetuse-dev 実環境へ
デプロイして実施した E2E の証跡です（あなたはコードを実行できないため Claude が残したもの）。
その証跡が diff の主張を裏づけているか、複数シナリオ（最低2本）を網羅しているか、未実施範囲に
正当な理由（SKIPPED.md）があるかも評価し、証跡が無い/不十分なまま完了を主張していれば指摘すること。
各指摘には severity(blocker|major|minor) と file・line・issue・suggestion を付けること。
blocker が1件でもあれば verdict は必ず FAIL。blocker が0件なら PASS。
e2e セクションがあれば、実行シナリオ・結果・証跡パス・十分性の所見を出力スキーマの e2e に記すこと。
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
# 既定の Codex レビューモデル。CODEX_MODEL を明示指定すれば上書き可能。
CODEX_MODEL="${CODEX_MODEL:-gpt-5.6-sol}"
if [ -n "${CODEX_MODEL:-}" ]; then
  CODEX_ARGS+=(--model "$CODEX_MODEL")
fi

# --- Codex 入力ペイロード = diff ＋ 実環境 E2E 証跡（あれば） --------------
# Codex は read-only でコードを実行できないため、完了ゲートで Claude が残した
# runs/<run-id>/e2e/ の証跡を添付して「証跡＋diff」を評価させる。
E2E_DIR="runs/${RUN_ID}/e2e"
PAYLOAD="${REV_DIR}/review-${N}.payload.txt"
{
  printf '%s\n' "$DIFF"
  printf '\n\n===== 実環境 E2E 証跡 (jetuse-dev / Codex は実行せず証跡を評価する) =====\n'
  if [ -d "$E2E_DIR" ] && [ -n "$(ls -A "$E2E_DIR" 2>/dev/null)" ]; then
    find "$E2E_DIR" -type f | sort | while read -r ef; do
      printf -- '--- %s ---\n' "$ef"
      tail -c 8000 "$ef"
      printf '\n'
    done
  else
    printf '(証跡なし: %s が空。デプロイ/E2E 未実施または対象外。完了主張ならその妥当性を厳しく見ること)\n' "$E2E_DIR"
  fi
} > "$PAYLOAD"

set +e
codex "${CODEX_ARGS[@]}" >"$RAW" 2>&1 < "$PAYLOAD"
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
