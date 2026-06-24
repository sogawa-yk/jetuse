#!/usr/bin/env bash
# 開発者ごとのContainer Instanceを停止/再開する(短時間アイドル用。再applyなしで課金停止)。
# CIが唯一の従量コスト要因。長期間使わないなら dev-env-down.sh で破棄する方が確実。
# 使い方: ops/dev-env-stop.sh <dev>          # 停止
#         ops/dev-env-stop.sh <dev> --start  # 再開
set -euo pipefail
cd "$(dirname "$0")/.."

DEV="${1:?usage: dev-env-stop.sh <dev> [--start]}"
ACTION=STOP
[ "${2:-}" = "--start" ] && ACTION=START

COMP=$(grep '^COMPARTMENT_OCID=' .env | cut -d= -f2-)
CID=$(oci container-instances container-instance list \
  --compartment-id "$COMP" --display-name "jetuse-${DEV}-api" \
  --lifecycle-state ACTIVE --query 'data.items[0].id' --raw-output 2>/dev/null || true)
[ -n "${CID:-}" ] && [ "$CID" != "null" ] || CID=$(oci container-instances container-instance list \
  --compartment-id "$COMP" --display-name "jetuse-${DEV}-api" \
  --query 'data.items[0].id' --raw-output)
[ -n "$CID" ] && [ "$CID" != "null" ] || { echo "container instance jetuse-${DEV}-api not found"; exit 1; }

echo "== ${ACTION} ${CID}"
oci container-instances container-instance action --container-instance-id "$CID" --action "$ACTION"
echo "done"
