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
# 直前の api 失敗が「存在しない(404)」かどうか。401/403/429/5xx/通信断と区別する。
was_not_found() { grep -qiE '\b404\b|NotAuthorizedOrNotFound' "$TMP/err"; }
pick_state() { grep -oE '"lifecycleState": "[A-Z_]+"' "$TMP/resp" | head -1 | sed -E 's/.*"([A-Z_]+)"/\1/'; }

# 削除対象は ACTIVE とは限らない（作成途中・FAILED・DELETING で残ることがある）。
# lifecycleState を列挙して問い合わせると未列挙の状態を取りこぼすので、
# 同名を全部引いてから DELETED 以外を選ぶ(review F-004)。
api --http-method GET \
  --target-uri "$BASE/hostedApplications?compartmentId=$HA_COMPARTMENT&displayName=$HA_NAME" ||
  fail "Hosted Application の一覧取得に失敗しました。destroy を中断します（リソースを残したまま state を消さないため）"
APP="$(tr -d ' \n' < "$TMP/resp" | sed 's/},{/}\n{/g' |
  grep -v '"lifecycleState":"DELETED"' |
  grep -oE '"id":"ocid1\.generativeaihostedapplication[^"]*"' | head -1 |
  sed -E 's/.*"(ocid1[^"]*)"/\1/')"

if [ -z "$APP" ]; then
  # 一覧の取得に成功したうえで該当なし＝本当に無い。
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
  elif was_not_found; then
    exit 0 # 404＝本当に消えた
  else
    # 401/403/429/5xx/通信断を「消えた」と扱うと、実体を残したまま state から
    # terraform_data だけが消え、Terraform から回収できなくなる(review F-001)。
    fail "Hosted Application $HA_NAME の削除確認に失敗しました。destroy を中断します"
  fi
  sleep 12
done

fail "Hosted Application $HA_NAME が DELETED になりませんでした"
