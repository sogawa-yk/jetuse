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

APPLY=0; DEV=""
for a in "$@"; do case "$a" in --apply) APPLY=1;; -*) echo "unknown flag: $a" >&2; exit 2;; *) DEV="$a";; esac; done
[ -n "$DEV" ] || { echo "usage: dev-env-up.sh <dev> [--apply]"; exit 1; }
APPDIR=infra/terraform/environments/app
TFVARS="$APPDIR/${DEV}.tfvars"
[ -f "$TFVARS" ] || { echo "missing $TFVARS (copy alice.tfvars.example)"; exit 1; }

# OCIR の名前空間はテナンシ固有なので**リポジトリに埋めない**（規約）。
# Object Storage の名前空間と同じ値なので、.env の OS_NAMESPACE を既定に使う。
# **.env は source していない**（shell 変数として export されない）ので、
# 環境変数ではなくファイルから読む。優先順: 環境変数 > .env の OCIR_NAMESPACE > .env の OS_NAMESPACE。
_ns_from_env_file() { grep "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"'\r'; }
NS="${OCIR_NAMESPACE:-$(_ns_from_env_file OCIR_NAMESPACE)}"
[ -n "$NS" ] || NS="$(_ns_from_env_file OS_NAMESPACE)"
# どちらも無いなら止める（誤ったテナンシの repository を掴まないため）
[ -n "$NS" ] || { echo "OCIR_NAMESPACE か OS_NAMESPACE を .env に設定してください" >&2; exit 1; }
SHA=$(git rev-parse --short HEAD)
TAG="dev-${DEV}-${SHA}"
# イメージの置き場は**このスタックの配備先リージョン**に合わせる(AGT-06)。
# 正は <dev>.tfvars の region(terraform がそれで配備するため)。無ければ .env / 既定。
# kix.ocir.io を直書きしていたときは、region=us-chicago-1 にしても
# **イメージだけ大阪へ push され**、シカゴのコンテナが pull できなかった。
. "$(dirname "$0")/_region.sh"
REGION=$(jetuse_region_from_tfvars "$TFVARS")
REGION="${REGION:-$(jetuse_region)}"
jetuse_use_cli_region "$REGION"
IMAGE="$(jetuse_ocir_host "$REGION")/${NS}/jetuse-dev-api:${TAG}"
echo "== region=${REGION} (tfvars 由来) / image registry=$(jetuse_ocir_host "$REGION")"

. "$(dirname "$0")/_container.sh"
CE=$(jetuse_container_engine) || exit 1
echo "== build & push ${IMAGE} (engine=${CE})"
# ビルドコンテキストはリポジトリルート(Containerfile が packages/jetuse_shared を取り込むため。P1b)
"$CE" build -f packages/api/Containerfile -t "${IMAGE}" .
"$CE" push "${IMAGE}"

echo "== terraform plan (state: ${DEV}.tfstate)"
( cd "$APPDIR"
  terraform init -input=false >/dev/null
  terraform plan -input=false -var-file="${DEV}.tfvars" \
    -var "api_image_url=${IMAGE}" -state="${DEV}.tfstate" -out="${DEV}.tfplan"
)
# CLAUDE.md: terraform apply は承認ゲート。ヘッドレス安全のため対話確認はせず、--apply 明示時のみ適用する。
if [ "$APPLY" -ne 1 ]; then
  echo "== plan のみ完了（適用するには --apply を渡す）。SPA 配信もスキップ。"
  exit 0
fi
( cd "$APPDIR" && terraform apply -input=false -state="${DEV}.tfstate" "${DEV}.tfplan" )

echo "== build & deploy SPA -> jetuse-${DEV}-spa"
( cd packages/web && npm run build && bash scripts/deploy.sh "jetuse-${DEV}-spa" )

HOST=$(cd "$APPDIR" && terraform output -state="${DEV}.tfstate" -raw apigw_hostname)
echo ""
echo "== done: https://${HOST}/"
echo "   API: https://${HOST}/api/chat/models"
