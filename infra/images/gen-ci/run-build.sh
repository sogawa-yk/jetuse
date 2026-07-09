#!/bin/sh
# 相2 信頼ビルド — jetuse-builder-build-<job_id> の PID 1(SP3-08 / ADR-0023 §1)。
# 入力(env): SRC_URL(API 検証済み src の読取専用 PAR)/ PLAN_URL(読取専用 PAR)/
#            OUT_URL / LOG_URL(書込専用 PAR)/ VITE_DEMO_MODEL / PHASE_TIMEOUT_S。
# OpenCode/LLM 非搭載。node_modules はイメージ焼き込み(実行時 npm 解決なし — ADR §3)。
# 保護原本(client.js 等)はイメージ内クリーン scaffold のもの(API は検証 src から保護
# パスを落として渡すため、ここで上書きされない — 層0)。
# ログは常に LOG_URL へ PUT(N4 — INACTIVE 後の retrieve-logs 409 を実機確認済み)。
set -eu
LOG=/tmp/phase.log
: > "$LOG"
upload_log() { node /usr/local/bin/xfer.mjs put "$LOG_URL" "$LOG" >/dev/null 2>&1 || true; }
trap upload_log EXIT
exec >"$LOG" 2>&1
mkdir -p /work
cp -a /scaffold/. /work/
cd /work
node /usr/local/bin/xfer.mjs get "$SRC_URL" /tmp/src.tgz
node /usr/local/bin/xfer.mjs get "$PLAN_URL" demo-plan.json
mkdir -p src
tar -xzf /tmp/src.tgz -C src --no-same-owner
timeout -s KILL "$PHASE_TIMEOUT_S" node node_modules/vite/bin/vite.js build
tar -czf /tmp/dist.tgz -C dist .
node /usr/local/bin/xfer.mjs put "$OUT_URL" /tmp/dist.tgz
echo "[build] artifact uploaded"
