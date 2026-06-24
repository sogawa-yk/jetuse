#!/usr/bin/env bash
# SPAデプロイ: dist/ を jetuse-dev-spa バケットへ content-type 付きでアップロードする
# (ADR-0004: Object StorageはメタデータのContent-Typeをそのまま返すため指定必須)
# 使い方: npm run build && bash scripts/deploy.sh [bucket]
set -euo pipefail
cd "$(dirname "$0")/.."

BUCKET="${1:-jetuse-dev-spa}"
[ -f dist/index.html ] || { echo "dist/ がありません。先に npm run build を実行してください" >&2; exit 1; }

content_type() {
  case "$1" in
    *.html) echo "text/html; charset=utf-8" ;;
    *.js | *.mjs) echo "text/javascript; charset=utf-8" ;;
    *.css) echo "text/css; charset=utf-8" ;;
    *.json) echo "application/json; charset=utf-8" ;;
    *.svg) echo "image/svg+xml" ;;
    *.png) echo "image/png" ;;
    *.ico) echo "image/x-icon" ;;
    *.woff2) echo "font/woff2" ;;
    *.map) echo "application/json" ;;
    *) echo "application/octet-stream" ;;
  esac
}

# ハッシュ付きアセットは長期キャッシュ可、index.html等は都度再検証
cache_control() {
  case "$1" in
    assets/*) echo "public, max-age=31536000, immutable" ;;
    *) echo "no-cache" ;;
  esac
}

count=0
while IFS= read -r -d '' f; do
  rel="${f#dist/}"
  oci os object put -bn "$BUCKET" --name "$rel" --file "$f" --force \
    --content-type "$(content_type "$rel")" \
    --cache-control "$(cache_control "$rel")" >/dev/null
  echo "put: $rel"
  count=$((count + 1))
done < <(find dist -type f -print0)
echo "done: ${count} objects -> ${BUCKET}"
