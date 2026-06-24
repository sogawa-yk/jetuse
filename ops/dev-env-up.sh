#!/usr/bin/env bash
# 開発者ごとのE2E環境を作成/更新する(共有基盤は environments/dev のまま流用)。
# APIイメージを本人タグでbuild/push → app스タックをterraform apply → SPAをbuild/配信 → URL表示。
# 前提:
#   - 共有 environments/dev を一度 `terraform apply` 済み(新出力が state に反映されている)
#   - 本人スキーマを ops/setup-dev-schema.py --dev <dev> で作成済み
#   - infra/terraform/environments/app/<dev>.tfvars を用意済み(alice.tfvars.example 参照)
#   - OCIRログイン済み / .env に OCIR_TOKEN 等
# 使い方: ops/dev-env-up.sh <dev>
set -euo pipefail
cd "$(dirname "$0")/.."

DEV="${1:?usage: dev-env-up.sh <dev>}"
APPDIR=infra/terraform/environments/app
TFVARS="$APPDIR/${DEV}.tfvars"
[ -f "$TFVARS" ] || { echo "missing $TFVARS (copy alice.tfvars.example)"; exit 1; }

NS=$(grep '^OS_NAMESPACE=' .env | cut -d= -f2- || true)
NS="${NS:-idqcucnenh88}"
SHA=$(git rev-parse --short HEAD)
TAG="dev-${DEV}-${SHA}"
IMAGE="kix.ocir.io/${NS}/jetuse-dev-api:${TAG}"

echo "== build & push ${IMAGE}"
# ビルドコンテキストはリポジトリルート(Containerfile が packages/jetuse_shared を取り込むため。P1b)
podman build -f packages/api/Containerfile -t "${IMAGE}" .
podman push "${IMAGE}"

echo "== terraform plan (state: ${DEV}.tfstate)"
( cd "$APPDIR"
  terraform init -input=false >/dev/null
  terraform plan -input=false -var-file="${DEV}.tfvars" \
    -var "api_image_url=${IMAGE}" -state="${DEV}.tfstate" -out="${DEV}.tfplan"
)
# CLAUDE.md: terraform apply は承認ゲート。明示確認してから適用する。
read -r -p "上記planを適用しますか? [y/N] " ans
[ "$ans" = "y" ] || { echo "中止"; exit 1; }
( cd "$APPDIR" && terraform apply -input=false -state="${DEV}.tfstate" "${DEV}.tfplan" )

echo "== build & deploy SPA -> jetuse-${DEV}-spa"
( cd packages/web && npm run build && bash scripts/deploy.sh "jetuse-${DEV}-spa" )

HOST=$(cd "$APPDIR" && terraform output -state="${DEV}.tfstate" -raw apigw_hostname)
echo ""
echo "== done: https://${HOST}/"
echo "   API: https://${HOST}/api/chat/models"
