#!/bin/sh
# PORT-03: ホスト型エージェントを削除する（destroy-time provisioner から呼ばれる）。
#
# ACTIVE な Hosted Deployment は直接削除できず、Application を削除すると
# デプロイメントがカスケード削除される（ops/recreate-agents.sh の実機記録）。
#
# 入力（環境変数）: HA_REGION HA_COMPARTMENT HA_NAME HA_OWNER_TAG
#
# 所有者タグが一致しないものには触れない。prefix が既存リソースと衝突していても、
# このスタックが作っていない Hosted Application を削除しない(review F-001)。
set -eu

BASE="https://generativeai.${HA_REGION}.oci.oraclecloud.com/20231130"
api() { oci raw-request --region "$HA_REGION" "$@"; }
pick_ocid() { grep -oE '"id": "ocid1\.generativeaihostedapplication[^"]*"' | head -1 | sed -E 's/.*"(ocid1[^"]*)"/\1/'; }
pick_state() { grep -oE '"lifecycleState": "[A-Z_]+"' | head -1 | sed -E 's/.*"([A-Z_]+)"/\1/'; }

# 削除対象は ACTIVE とは限らない（作成途中や FAILED で残ることがある）ので状態で絞らない。
APP=""
for st in ACTIVE CREATING FAILED NEEDS_ATTENTION; do
  found="$(api --http-method GET \
    --target-uri "$BASE/hostedApplications?compartmentId=$HA_COMPARTMENT&displayName=$HA_NAME&lifecycleState=$st" \
    2>/dev/null | pick_ocid || true)"
  [ -n "$found" ] && { APP="$found"; break; }
done

if [ -z "$APP" ]; then
  echo "Hosted Application $HA_NAME は既に存在しません"
  exit 0
fi

if ! api --http-method GET --target-uri "$BASE/hostedApplications/$APP" 2>/dev/null |
  tr -d ' \n' | grep -q "\"jetuse-owner\":\"$HA_OWNER_TAG\""; then
  echo "Hosted Application $HA_NAME はこのスタックが作ったものではないため削除しません" >&2
  exit 0
fi

api --http-method DELETE --target-uri "$BASE/hostedApplications/$APP" >/dev/null

# DELETED でも GET は 200 を返す（404 とは限らない）ので lifecycleState で判定する。
# パイプの最後のコマンドが成功すると GET の失敗が隠れるため、GET の成否は if で見る
# （`... || echo GONE` は pipefail 無しでは機能しない — review F-006）。
i=0
while [ "$i" -lt 60 ]; do
  i=$((i + 1))
  if body="$(api --http-method GET --target-uri "$BASE/hostedApplications/$APP" 2>/dev/null)"; then
    [ "$(printf '%s' "$body" | pick_state)" = DELETED ] && exit 0
  else
    exit 0 # GET 自体が失敗＝もう無い
  fi
  sleep 12
done

echo "Hosted Application $HA_NAME が DELETED になりませんでした" >&2
exit 1
