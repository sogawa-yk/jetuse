#!/bin/sh
# 相1 生成(非信頼)— jetuse-builder-gen-<job_id> の PID 1(SP3-08 / ADR-0023 §1)。
# 入力(env): PLAN_URL / CONFIG_URL = 読取専用 PAR、OUT_URL / LOG_URL = 書込専用 PAR、
#            GEN_MODEL(oci/<oci_id>)、GEN_PROMPT、PHASE_TIMEOUT_S。
# この CI には OCI 資格情報も RP も無い(is_resource_principal_disabled — S2)。
# 成功時のみ最後に src/ を tar にして PUT する(成果物の存在 = 成功判定)。
# ログは常に LOG_URL へ PUT する(N4 — コンテナ INACTIVE 後は retrieve-logs が 409 で
# 取得不能と実機確認済みのため、コンテナ自身が終了時に書き出す。trap = 失敗時も残す)。
set -eu
LOG=/tmp/phase.log
: > "$LOG"
upload_log() { node /usr/local/bin/xfer.mjs put "$LOG_URL" "$LOG" >/dev/null 2>&1 || true; }
trap upload_log EXIT
exec >"$LOG" 2>&1
mkdir -p /work
cp -a /scaffold/. /work/
cd /work
node /usr/local/bin/xfer.mjs get "$PLAN_URL" demo-plan.json
node /usr/local/bin/xfer.mjs get "$CONFIG_URL" opencode.json
# HOME=/gen-home: provider npm(@ai-sdk/openai)事前導入済みキャッシュ(イメージ焼き込み)
export HOME=/gen-home
timeout -s KILL "$PHASE_TIMEOUT_S" opencode run --model "$GEN_MODEL" "$GEN_PROMPT"
tar -czf /tmp/src.tgz -C /work/src .
node /usr/local/bin/xfer.mjs put "$OUT_URL" /tmp/src.tgz
echo "[gen] artifact uploaded"
