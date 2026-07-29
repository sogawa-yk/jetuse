#!/bin/sh
# PORT-03: ホスト型エージェントを削除する（destroy-time provisioner から呼ばれる）。
#
# ACTIVE な Hosted Deployment は直接削除できず、Application を削除すると
# デプロイメントがカスケード削除される（ops/recreate-agents.sh の実機記録）。
#
# 入力（環境変数）: HA_REGION HA_COMPARTMENT HA_NAME HA_OWNER_TAG
#
# 方針:
# - 所有者タグが一致しないものには触れない。prefix が既存リソースと衝突していても、
#   このスタックが作っていない Hosted Application を削除しない(review F-001)。
# - **API 失敗を「もう無い」と取り違えない**。取り違えると、実リソースが残ったまま
#   state からだけ消えて Terraform から回収できない孤児になる(review F-002)。
#   所有権を確認できないときも destroy を失敗させ、state を残して再試行可能にする。
set -eu

BASE="https://generativeai.${HA_REGION}.oci.oraclecloud.com/20231130"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

api() { oci raw-request --region "$HA_REGION" "$@" > "$TMP/resp" 2>"$TMP/err"; }
fail() { echo "$1" >&2; [ -s "$TMP/err" ] && sed -n '1,5p' "$TMP/err" >&2; exit 1; }
pick_ocid() { grep -oE '"id": "ocid1\.generativeaihostedapplication[^"]*"' "$TMP/resp" | head -1 | sed -E 's/.*"(ocid1[^"]*)"/\1/'; }
pick_state() { grep -oE '"lifecycleState": "[A-Z_]+"' "$TMP/resp" | head -1 | sed -E 's/.*"([A-Z_]+)"/\1/'; }

# 削除対象は ACTIVE とは限らない（作成途中や FAILED で残ることがある）ので状態で絞らない。
APP=""
for st in ACTIVE CREATING FAILED NEEDS_ATTENTION; do
  api --http-method GET \
    --target-uri "$BASE/hostedApplications?compartmentId=$HA_COMPARTMENT&displayName=$HA_NAME&lifecycleState=$st" ||
    fail "Hosted Application の一覧取得に失敗しました（$st）。destroy を中断します（リソースを残したまま state を消さないため）"
  found="$(pick_ocid)"
  [ -n "$found" ] && { APP="$found"; break; }
done

if [ -z "$APP" ]; then
  # 一覧はすべて成功して該当なし＝本当に無い。
  echo "Hosted Application $HA_NAME は既に存在しません"
  exit 0
fi

api --http-method GET --target-uri "$BASE/hostedApplications/$APP" ||
  fail "Hosted Application $HA_NAME の取得に失敗し、所有権を確認できませんでした。destroy を中断します"

if ! tr -d ' \n' < "$TMP/resp" | grep -q "\"jetuse-owner\":\"$HA_OWNER_TAG\""; then
  echo "Hosted Application $HA_NAME はこのスタックが作ったものではないため削除しません"
  exit 0
fi

api --http-method DELETE --target-uri "$BASE/hostedApplications/$APP" ||
  fail "Hosted Application $HA_NAME の削除要求に失敗しました"

# DELETED でも GET は 200 を返す（404 とは限らない）ので lifecycleState で判定する。
i=0
while [ "$i" -lt 60 ]; do
  i=$((i + 1))
  if api --http-method GET --target-uri "$BASE/hostedApplications/$APP"; then
    [ "$(pick_state)" = DELETED ] && exit 0
  else
    # 削除要求が受理された後に取得できなくなった＝消えたとみなす。
    exit 0
  fi
  sleep 12
done

fail "Hosted Application $HA_NAME が DELETED になりませんでした"
