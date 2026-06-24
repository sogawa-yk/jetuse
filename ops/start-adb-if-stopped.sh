#!/usr/bin/env bash
# 夜間停止後にjetuse-dev-adbがSTOPPEDのままになる問題(backlog #10)の暫定対策。
# dev計算インスタンスのopcユーザーcronから毎朝実行する想定(導入は人間判断)。
# 例: crontab -e で「30 8 * * 1-5 /home/opc/jetuse/ops/start-adb-if-stopped.sh >> /tmp/adb-start.log 2>&1」
set -euo pipefail
COMP=$(grep '^COMPARTMENT_OCID=' "$(dirname "$0")/../.env" | cut -d= -f2)
ADB=$(oci db autonomous-database list -c "$COMP" \
  --query 'data[?"display-name"==`jetuse-dev-adb` && "lifecycle-state"==`STOPPED`].id | [0]' --raw-output)
if [ -z "$ADB" ] || [ "$ADB" = "null" ]; then
  echo "$(date -Is) jetuse-dev-adb is not STOPPED — nothing to do"
  exit 0
fi
echo "$(date -Is) starting $ADB"
oci db autonomous-database start --autonomous-database-id "$ADB" >/dev/null
echo "$(date -Is) start requested"
