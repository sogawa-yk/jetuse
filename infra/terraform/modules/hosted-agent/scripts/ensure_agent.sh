#!/bin/sh
# PORT-03: ホスト型エージェント(Hosted Application + Deployment)を1つ用意する。
#
# なぜ Terraform の resource ではなくこのスクリプトなのかは main.tf 冒頭のコメント参照
# (provider 8.24.0 は work request の完了判定を誤り、必ず失敗する)。
#
# 入力はすべて環境変数。コマンド本文へ値を内挿しないので、利用者入力(image_tag 等)に
# `$(...)` や引用符が入っていてもシェル実行や JSON 破壊は起きない(review F-003)。
#   HA_REGION HA_COMPARTMENT HA_NAME HA_SDK HA_IDCS_ENDPOINT HA_AUDIENCE HA_SCOPE
#   HA_MIN_REPLICA HA_MAX_REPLICA HA_CONCURRENCY HA_CONTAINER_URI HA_TAG HA_OWNER_TAG
#   JETUSE_AGENT_CONFIG (コンテナへ渡す設定 JSON)
#
# 冪等: 自分が作った(所有者タグ付きの)ACTIVE なアプリがあれば再利用する。
# 自己修復: 同名で FAILED 等になった自分のアプリが残っていれば削除してから作り直す
#           (create 時に失敗した terraform_data は tainted になり destroy provisioner が
#            走らないため、放置すると次回も同じ状態を拾って失敗し続ける — review F-004)。
set -eu

BASE="https://generativeai.${HA_REGION}.oci.oraclecloud.com/20231130"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

api() { oci raw-request --region "$HA_REGION" "$@"; }

# raw-request は --query を無視する(2026-07-29 実測)ので抽出は grep で行う。
pick_ocid() { grep -oE '"id": "ocid1\.'"$1"'[^"]*"' | head -1 | sed -E 's/.*"(ocid1[^"]*)"/\1/'; }
pick_state() { grep -oE '"lifecycleState": "[A-Z_]+"' | head -1 | sed -E 's/.*"([A-Z_]+)"/\1/'; }

# 自分が作ったものか(所有者タグの有無)。取り込み・削除の前に必ず通す。
owned() { # owned <app-ocid>
  api --http-method GET --target-uri "$BASE/hostedApplications/$1" 2>/dev/null |
    tr -d ' \n' | grep -q "\"jetuse-owner\":\"$HA_OWNER_TAG\""
}

find_app() { # find_app <lifecycleState>
  api --http-method GET \
    --target-uri "$BASE/hostedApplications?compartmentId=$HA_COMPARTMENT&displayName=$HA_NAME&lifecycleState=$1" \
    2>/dev/null | pick_ocid generativeaihostedapplication || true
}

wait_deleted() { # wait_deleted <app-ocid>
  i=0
  while [ "$i" -lt 60 ]; do
    i=$((i + 1))
    if body="$(api --http-method GET --target-uri "$BASE/hostedApplications/$1" 2>/dev/null)"; then
      [ "$(printf '%s' "$body" | pick_state)" = DELETED ] && return 0
    else
      return 0 # GET 自体が失敗＝もう無い
    fi
    sleep 12
  done
  return 1
}

# --- 1) 既存の自分のアプリを探す（無ければ作る） ---
APP="$(find_app ACTIVE)"
if [ -n "$APP" ] && ! owned "$APP"; then
  echo "同名の Hosted Application ($HA_NAME) がありますが、このスタックが作ったものではありません。" >&2
  echo "prefix を変更するか、既存リソースを確認してください（既存リソースには触れません）。" >&2
  exit 1
fi

if [ -z "$APP" ]; then
  # 失敗して残った自分のアプリがあれば掃除してから作り直す。
  for st in FAILED CREATING; do
    stale="$(find_app "$st")"
    if [ -n "$stale" ] && owned "$stale"; then
      echo "掃除: $HA_NAME が $st のまま残っているため削除します"
      api --http-method DELETE --target-uri "$BASE/hostedApplications/$stale" >/dev/null 2>&1 || true
      wait_deleted "$stale" || true
    fi
  done

  # 設定 JSON を JSON 文字列として埋め込むためエスケープする(python 等に依存しない)。
  ESCAPED="$(printf '%s' "$JETUSE_AGENT_CONFIG" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
  cat > "$TMP/app.json" <<JSON
{"displayName":"$HA_NAME","compartmentId":"$HA_COMPARTMENT",
 "description":"JetUse ReAct agent container ($HA_SDK SDK)",
 "freeformTags":{"jetuse-owner":"$HA_OWNER_TAG"},
 "scalingConfig":{"scalingType":"CONCURRENCY","minReplica":$HA_MIN_REPLICA,"maxReplica":$HA_MAX_REPLICA,"targetConcurrencyThreshold":$HA_CONCURRENCY},
 "inboundAuthConfig":{"inboundAuthConfigType":"IDCS_AUTH_CONFIG","idcsConfig":{"domainUrl":"$HA_IDCS_ENDPOINT","audience":"$HA_AUDIENCE","scope":"$HA_SCOPE"}},
 "environmentVariables":[{"name":"JETUSE_AGENT_CONFIG","type":"PLAINTEXT","value":"$ESCAPED"}]}
JSON
  APP="$(api --http-method POST --target-uri "$BASE/hostedApplications" --request-body "file://$TMP/app.json" | pick_ocid generativeaihostedapplication)"
  [ -n "$APP" ] || { echo "Hosted Application $HA_NAME の作成に失敗しました" >&2; exit 1; }

  i=0; ST=""
  while [ "$i" -lt 60 ]; do
    i=$((i + 1))
    ST="$(api --http-method GET --target-uri "$BASE/hostedApplications/$APP" | pick_state)"
    [ "$ST" = ACTIVE ] && break
    case "$ST" in
      FAILED | DELETED)
        echo "Hosted Application $HA_NAME が $ST になりました（inbound の domain URL が実在するか確認してください）" >&2
        exit 1
        ;;
    esac
    sleep 10
  done
  [ "$ST" = ACTIVE ] || { echo "Hosted Application $HA_NAME が ACTIVE になりませんでした" >&2; exit 1; }
fi

# --- 2) デプロイメント（イメージ pull + 脆弱性スキャンを伴うため時間がかかる） ---
# 1アプリ=1デプロイメントなので、既にあれば作らない。
DEP="$(api --http-method GET --target-uri "$BASE/hostedDeployments?compartmentId=$HA_COMPARTMENT&applicationId=$APP" 2>/dev/null | pick_ocid generativeaihosteddeployment || true)"
if [ -z "$DEP" ]; then
  cat > "$TMP/dep.json" <<JSON
{"displayName":"$HA_NAME-dep","compartmentId":"$HA_COMPARTMENT","hostedApplicationId":"$APP",
 "freeformTags":{"jetuse-owner":"$HA_OWNER_TAG"},
 "activeArtifact":{"artifactType":"SIMPLE_DOCKER_ARTIFACT","containerUri":"$HA_CONTAINER_URI","tag":"$HA_TAG"}}
JSON
  DEP="$(api --http-method POST --target-uri "$BASE/hostedDeployments" --request-body "file://$TMP/dep.json" | pick_ocid generativeaihosteddeployment)"
  [ -n "$DEP" ] || { echo "$HA_NAME の Hosted Deployment 作成に失敗しました" >&2; exit 1; }
fi

i=0; ST=""
while [ "$i" -lt 120 ]; do
  i=$((i + 1))
  ST="$(api --http-method GET --target-uri "$BASE/hostedDeployments/$DEP" | pick_state)"
  [ "$ST" = ACTIVE ] && break
  case "$ST" in
    FAILED | NEEDS_ATTENTION | DELETED)
      echo "$HA_NAME の Hosted Deployment が $ST になりました（イメージ $HA_CONTAINER_URI:$HA_TAG が存在するか確認してください）" >&2
      exit 1
      ;;
  esac
  sleep 15
done
[ "$ST" = ACTIVE ] || { echo "$HA_NAME の Hosted Deployment が30分以内に ACTIVE になりませんでした" >&2; exit 1; }

echo "hosted agent ready: $HA_NAME"
