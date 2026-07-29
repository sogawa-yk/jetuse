#!/bin/sh
# PORT-03: ensure_agent.sh / delete_agent.sh の分岐をモック CLI で確認する。
# terraform test は local-exec を実行しないため、シェル側の判断はここで固定する。
#
# 使い方: sh infra/terraform/modules/hosted-agent/scripts/smoke_test.sh
#
# 実際にここで捕まえた不具合（回帰防止）:
# - `for x in $(list_live_apps)` だと一覧取得の失敗が **サブシェル内の exit** で終わり、
#   呼び出し元は「該当なし」として成功終了していた（destroy が実体を残して state だけ消す）。
# - 同名候補の先頭が管理外だと、`head -1` では自分のリソースを取り違えていた。
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
bin="$(mktemp -d)"
trap 'rm -rf "$bin"' EXIT
cp "$here/mock_oci_for_tests.sh" "$bin/oci"
chmod +x "$bin/oci"

pass=0
fail=0
check() { # check <名前> <期待exit> <MOCK_CASE> <期待文言>
  out="$(MOCK_CASE="$3" PATH="$bin:$PATH" HA_REGION=us-chicago-1 HA_COMPARTMENT=comp \
        HA_NAME=jetuse-p03-agent-openai HA_OWNER_TAG='jetuse:p03' HA_CONFIG_FINGERPRINT=FP \
        sh "$here/delete_agent.sh" 2>&1)" && rc=0 || rc=$?
  if [ "$rc" = "$2" ] && printf '%s' "$out" | grep -q "$4"; then
    echo "PASS  $1"; pass=$((pass + 1))
  else
    echo "FAIL  $1 (exit=$rc): $(printf '%s' "$out" | head -1)"; fail=$((fail + 1))
  fi
}

check "一覧取得の失敗で destroy を中断する" 1 list_fail "一覧取得に失敗"
check "管理外の同名リソースには触れない" 0 foreign "このスタックが作ったものではない"

echo "=== ${pass} passed, ${fail} failed ==="
[ "$fail" = 0 ]
