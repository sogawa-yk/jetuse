#!/bin/sh
# PORT-03: ensure_agent.sh / delete_agent.sh の分岐をモック CLI で確認する。
# terraform test は local-exec を実行しないため、シェル側の判断はここで固定する。
#
# 使い方: sh infra/terraform/modules/hosted-agent/scripts/smoke_test.sh
#
# 検査は「終了コード・出力の文言・**呼ばれた API**」の3点で行う。
# 消してはいけないものを消していないことは、応答ではなく呼び出しの不在でしか確かめられない。
#
# 実際にここで捕まえた不具合（回帰防止）:
# - `for x in $(list_live_apps)` だと一覧取得の失敗が **サブシェル内の exit** で終わり、
#   呼び出し元は「該当なし」として成功終了していた（destroy が実体を残して state だけ消す）。
# - 同名候補の先頭が管理外だと、`head -1` では自分のリソースを取り違えていた。
# - 所有候補を見つけた時点で走査を打ち切ると、所有アプリが複数残っていても
#   先頭だけ消して destroy が成功していた（残りが state 外の孤児になる）。
# - 一覧をページングしないと、2ページ目にある自分のリソースを「無い」と誤判断していた。
# - アプリ削除は配下のデプロイメントを巻き込むのに、削除経路で配下の所有権を見ていなかった。
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cp "$here/mock_oci_for_tests.sh" "$tmp/oci"
chmod +x "$tmp/oci"

# Terraform の jsonencode が組み立てる本文と同じ形（hostedApplicationId は placeholder）。
APP_BODY='{"compartmentId": "comp", "displayName": "jetuse-p03-agent-openai"}'
DEP_BODY='{"compartmentId": "comp", "hostedApplicationId":"__APP_OCID__", "activeArtifact": {"tag": "TAG"}}'

pass=0
fail=0
rc=0
out=""
log=""

# run <script> <MOCK_CASE> [<HA_TAG>] — スクリプトを1回走らせ、rc / out / log を残す。
run() {
  log="$tmp/log"
  : > "$log"
  out="$(MOCK_CASE="$2" MOCK_LOG="$log" PATH="$tmp:$PATH" \
    HA_REGION=us-chicago-1 HA_COMPARTMENT=comp HA_NAME=jetuse-p03-agent-openai \
    HA_OWNER_TAG='jetuse:p03' HA_CONFIG_FINGERPRINT=FP \
    HA_CONTAINER_URI=reg.example/jetuse-agent-openai HA_TAG="${3:-v1}" \
    HA_APP_BODY="$APP_BODY" HA_DEP_BODY="${DEP_BODY}" \
    sh "$here/$1" 2>&1)" && rc=0 || rc=$?
}

# check <名前> <期待exit> <期待文言> <呼ばれてはいけない正規表現|-> <呼ばれるべき正規表現|->
check() {
  why=""
  [ "$rc" = "$2" ] || why="exit=$rc (期待 $2)"
  printf '%s' "$out" | grep -q "$3" || why="${why:+$why / }出力に「$3」が無い"
  if [ "$4" != "-" ] && grep -qE "$4" "$log"; then
    why="${why:+$why / }呼ばれてはいけない API: $(grep -E "$4" "$log" | head -1)"
  fi
  if [ "$5" != "-" ] && ! grep -qE "$5" "$log"; then
    why="${why:+$why / }呼ばれるべき API が無い: $5"
  fi
  if [ -z "$why" ]; then
    echo "PASS  $1"
    pass=$((pass + 1))
  else
    echo "FAIL  $1: $why"
    printf '%s' "$out" | head -3 | sed 's/^/        /'
    fail=$((fail + 1))
  fi
}

# --- destroy 側 ---
run delete_agent.sh list_fail
check "一覧取得の失敗で destroy を中断する" 1 "一覧取得に失敗" '^DELETE' -

run delete_agent.sh foreign
check "管理外の同名リソースには触れない" 0 "このスタックが作ったものではない" '^DELETE' -

run delete_agent.sh owned_second
check "先頭が管理外でも自分のものを消す" 0 "deleted hosted agent" 'DELETE .*foreign$' 'DELETE .*\.\.mine$'

run delete_agent.sh owned_paged
check "2ページ目にある自分のものも見つける" 0 "deleted hosted agent" 'DELETE .*foreign$' 'DELETE .*\.\.mine$'

run delete_agent.sh owned_twice
check "所有アプリが複数なら消さずに中断する" 1 "2 件" '^DELETE' -

run delete_agent.sh foreign_dep
check "配下に管理外デプロイメントがあれば消さない" 1 "巻き込むため中断" '^DELETE' -

# --- apply 側 ---
run ensure_agent.sh foreign
check "同名の管理外アプリがあれば作らない" 1 "このスタックが作ったものではありません" '^(POST|DELETE)' -

run ensure_agent.sh reuse
check "設定一致なら作り直さず再利用する" 0 "reused" '^(POST|DELETE)' -

run ensure_agent.sh foreign_dep
check "配下が管理外なら再利用判定の手前で止まる" 1 "管理外です" '^(POST|DELETE)' -

run ensure_agent.sh foreign_dep_stale
check "作り直し経路でも管理外デプロイメントを巻き込まない" 1 "巻き込むため中断" '^(POST|DELETE)' -

run ensure_agent.sh stale_config
check "設定が変わったらアプリごと作り直す" 0 "hosted agent ready" 'DELETE .*foreign$' 'DELETE .*\.\.mine$'

# image_tag に placeholder と同じ文字列を入れても、置換対象は
# hostedApplicationId フィールドだけ（利用者入力で apply が壊れない）。
run ensure_agent.sh create_fresh '__APP_OCID__'
check "image_tag が placeholder と同じでも作成できる" 0 "hosted agent ready" \
  '"hostedApplicationId":"__APP_OCID__"' '"hostedApplicationId":"ocid1[^"]*fresh"'

echo "=== ${pass} passed, ${fail} failed ==="
[ "$fail" = 0 ]
