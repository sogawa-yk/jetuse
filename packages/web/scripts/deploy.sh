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
    *.webp) echo "image/webp" ;;
    *.jpg | *.jpeg) echo "image/jpeg" ;;
    *.txt) echo "text/plain; charset=utf-8" ;;
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

upload() {
  local rel="${1#dist/}"
  oci os object put -bn "$BUCKET" --name "$rel" --file "$1" --force \
    --content-type "$(content_type "$rel")" \
    --cache-control "$(cache_control "$rel")" >/dev/null
  echo "put: $rel"
  count=$((count + 1))
}

count=0
# index.html は最後に公開する(先に出すと未アップロードの新ハッシュ付きアセットを参照して
# 404 になる窓ができる — SP3-07 review M001)。旧世代のハッシュ付きアセットは削除しない
# (配信中クライアントの遅延チャンク読み込みを守る。ponytail: dev プレビューの肥大は許容 —
# 世代 GC が要るほど増えたら別途)
while IFS= read -r -d '' f; do
  upload "$f"
done < <(find dist -type f ! -name index.html -print0)
upload dist/index.html
echo "done: ${count} objects -> ${BUCKET}"
