#!/usr/bin/env bash
# 開発者ごとのE2E環境を破棄する(共有基盤 environments/dev/ADB等には触れない)。
# 破棄されるのは本人の Container Instance / API Gateway / SPAバケットのみ。
# 本人のADBスキーマ(JETUSE_<DEV>)はデータ保持のため残す(消す場合は手動 DROP USER)。
# 使い方: ops/dev-env-down.sh <dev>
set -euo pipefail
cd "$(dirname "$0")/.."

YES=0; DEV=""
for a in "$@"; do case "$a" in --yes) YES=1;; -*) echo "unknown flag: $a" >&2; exit 2;; *) DEV="$a";; esac; done
[ -n "$DEV" ] || { echo "usage: dev-env-down.sh <dev> --yes  (破壊的: terraform destroy)"; exit 1; }
APPDIR=infra/terraform/environments/app
[ -f "$APPDIR/${DEV}.tfvars" ] || { echo "missing $APPDIR/${DEV}.tfvars"; exit 1; }
if [ "$YES" -ne 1 ]; then
  echo "[dev-env-down] 破壊的操作（${DEV} の app スタックを terraform destroy）。実行するには --yes を渡す。"
  exit 1
fi

( cd "$APPDIR"
  terraform init -input=false >/dev/null
  terraform destroy -input=false -var-file="${DEV}.tfvars" \
    -var "api_image_url=unused" -state="${DEV}.tfstate"
)
echo "== destroyed app stack for ${DEV} (共有基盤・ADBスキーマは保持)"
