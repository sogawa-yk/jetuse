#!/usr/bin/env bash
# AGT-MULTI(ADR-0009 / P1b): 3SDK汎用ReActコンテナをビルドする。
#
# ビルドコンテキストはリポジトリルート。理由: 共有パッケージ jetuse_shared(SSRFガード/Webツール/
# SQLサニタイズ)は packages/jetuse_shared にあり agent-containers の外なので、Containerfile の COPY が
# 届くようコンテキストをルートに取る必要がある(各 Containerfile は packages/... のパスで COPY する)。
#
# 使い方:
#   packages/agent-containers/build.sh [タグ] [sdk...]
#   例: packages/agent-containers/build.sh 0.1.0 openai langgraph adk
# sdk 省略時は3つ全部。タグ既定 0.1.0。
set -euo pipefail
cd "$(dirname "$0")/../.."   # = リポジトリルート

TAG="${1:-0.1.0}"
shift || true
SDKS=("$@")
[ "${#SDKS[@]}" -eq 0 ] && SDKS=(openai langgraph adk)

for sdk in "${SDKS[@]}"; do
  echo "==== build jetuse-agent-$sdk:$TAG ===="
  podman build \
    -f "packages/agent-containers/Containerfile.$sdk" \
    -t "jetuse-agent-$sdk:$TAG" \
    .
done
echo "done: ${SDKS[*]}"
