#!/bin/sh
# PORT-03 スクリプトの経路確認用モック。MOCK_CASE で応答を切り替える。
#
# 呼ばれた API は $MOCK_LOG に "METHOD uri"（POST は続けて "BODY ..."）で追記する。
# 「消してはいけないものを消していない」ことは、応答ではなく **呼び出しの不在** でしか
# 確かめられないため、副作用の検査はこのログで行う。
uri=""
method=""
body=""
while [ $# -gt 0 ]; do
  case "$1" in
    --target-uri) uri="$2"; shift 2 ;;
    --http-method) method="$2"; shift 2 ;;
    --request-body) body="$2"; shift 2 ;;
    *) shift ;;
  esac
done

LOG="${MOCK_LOG:-/dev/null}"
printf '%s %s\n' "$method" "$uri" >> "$LOG"
if [ -n "$body" ]; then
  printf 'BODY %s\n' "$(tr -d ' \n' < "${body#file://}")" >> "$LOG"
fi

MINE=ocid1.generativeaihostedapplication.oc1..mine
MINE2=ocid1.generativeaihostedapplication.oc1..mine2
FOREIGN=ocid1.generativeaihostedapplication.oc1..foreign
FRESH=ocid1.generativeaihostedapplication.oc1..fresh
DEP_MINE=ocid1.generativeaihosteddeployment.oc1..depmine
DEP_FOREIGN=ocid1.generativeaihosteddeployment.oc1..depforeign

# 既に DELETE を投げた相手か（削除後の一覧・詳細を変えるため）。
deleted() { grep -q "^DELETE .*$1\$" "$LOG" 2>/dev/null; }

# 実際の `oci raw-request` はコロンの後に空白を置いた JSON を返す。スクリプト側の
# grep（pick_ocid / pick_state）はその形を前提にしているので、モックも同じ形で返す。
# item <ocid> [<state>]
item() { printf '{"id": "%s", "lifecycleState": "%s"}' "$1" "${2:-ACTIVE}"; }
# list <next-page> <items...>
list() {
  np="$1"
  shift
  printf '{"data": {"items": [%s]}, "headers": {%s}, "status": "200 OK"}\n' \
    "$(IFS=,; echo "$*")" \
    "$([ -n "$np" ] && printf '"opc-next-page": "%s"' "$np")"
}
# detail <ocid> <owner> [<state>] [<config-fp>] [<extra-json>]
detail() {
  printf '{"data": {"id": "%s", "lifecycleState": "%s", "freeformTags": {"jetuse-owner": "%s", "jetuse-config": "%s"}%s}, "headers": {}, "status": "200 OK"}\n' \
    "$1" "${3:-ACTIVE}" "$2" "${4:-FP}" "${5:-}"
}
# 自分のデプロイメント（再利用条件を満たす: containerUri と tag が入力と一致）。
dep_detail_mine() {
  detail "$DEP_MINE" "$HA_OWNER_TAG" "${1:-ACTIVE}" FP \
    ", \"activeArtifact\": {\"containerUri\": \"$HA_CONTAINER_URI\", \"tag\": \"$HA_TAG\"}"
}

case "$MOCK_CASE" in
  list_fail) # 一覧が権限エラー（不存在と区別できない）
    echo "ServiceError: NotAuthorizedOrNotFound status: 404" >&2
    exit 1
    ;;

  foreign) # 同名だが他人のアプリが1件だけ
    case "$uri" in
      *"displayName="*) list "" "$(item "$FOREIGN")" ;;
      *hostedApplications/*) detail "$FOREIGN" "jetuse:someone-else" ;;
    esac
    ;;

  owned_second) # 先頭が他人・2件目が自分（先頭1件だけ見ると取り違える）
    case "$uri" in
      *"displayName="*)
        if deleted "$MINE"; then list "" "$(item "$FOREIGN")"; else list "" "$(item "$FOREIGN")" "$(item "$MINE")"; fi ;;
      *"hostedApplications/$FOREIGN") detail "$FOREIGN" "jetuse:someone-else" ;;
      *"hostedApplications/$MINE")
        if deleted "$MINE"; then detail "$MINE" "$HA_OWNER_TAG" DELETED; else detail "$MINE" "$HA_OWNER_TAG"; fi ;;
      *hostedDeployments\?*) list "" "$(item "$DEP_MINE")" ;;
      *hostedDeployments/*) dep_detail_mine ;;
    esac
    ;;

  owned_paged) # 1ページ目に他人・2ページ目に自分（ページングしないと見落とす）
    case "$uri" in
      *"displayName="*page=P2*)
        if deleted "$MINE"; then list "" ""; else list "" "$(item "$MINE")"; fi ;;
      *"displayName="*) list P2 "$(item "$FOREIGN")" ;;
      *"hostedApplications/$FOREIGN") detail "$FOREIGN" "jetuse:someone-else" ;;
      *"hostedApplications/$MINE")
        if deleted "$MINE"; then detail "$MINE" "$HA_OWNER_TAG" DELETED; else detail "$MINE" "$HA_OWNER_TAG"; fi ;;
      *hostedDeployments\?*) list "" "$(item "$DEP_MINE")" ;;
      *hostedDeployments/*) dep_detail_mine ;;
    esac
    ;;

  owned_twice) # 自分のアプリが2件（どれが正か決められない）
    case "$uri" in
      *"displayName="*) list "" "$(item "$MINE")" "$(item "$MINE2")" ;;
      *hostedApplications/*) detail "${uri##*/}" "$HA_OWNER_TAG" ;;
    esac
    ;;

  foreign_dep | foreign_dep_stale) # 自分のアプリの配下に管理外デプロイメント（消すと巻き込む）
    # foreign_dep       … 設定は一致（apply は再利用判定へ進む）
    # foreign_dep_stale … 設定が不一致（apply はアプリごとの作り直しへ進む）
    fp=FP
    [ "$MOCK_CASE" = foreign_dep_stale ] && fp=OTHER_FP
    case "$uri" in
      *"displayName="*) list "" "$(item "$MINE")" ;;
      *"hostedApplications/$MINE") detail "$MINE" "$HA_OWNER_TAG" ACTIVE "$fp" ;;
      *hostedDeployments\?*) list "" "$(item "$DEP_FOREIGN")" ;;
      *hostedDeployments/*) detail "$DEP_FOREIGN" "jetuse:someone-else" ;;
    esac
    ;;

  reuse) # 自分のアプリ＋設定一致＋自分の ACTIVE デプロイメント → そのまま再利用
    case "$uri" in
      *"displayName="*) list "" "$(item "$MINE")" ;;
      *hostedApplications/*) detail "$MINE" "$HA_OWNER_TAG" ;;
      *hostedDeployments\?*) list "" "$(item "$DEP_MINE")" ;;
      *hostedDeployments/*) dep_detail_mine ;;
    esac
    ;;

  stale_config) # 設定指紋が違う（アプリごと作り直す経路。管理外デプロイメントは無い）
    case "$method$uri" in
      POST*hostedApplications) detail "$FRESH" "$HA_OWNER_TAG" ;;
      POST*hostedDeployments) dep_detail_mine ;;
      *"displayName="*)
        if deleted "$MINE"; then list "" ""; else list "" "$(item "$MINE")"; fi ;;
      *"hostedApplications/$FRESH") detail "$FRESH" "$HA_OWNER_TAG" ;;
      *"hostedApplications/$MINE")
        if deleted "$MINE"; then detail "$MINE" "$HA_OWNER_TAG" DELETED; else detail "$MINE" "$HA_OWNER_TAG" ACTIVE OTHER_FP; fi ;;
      *hostedDeployments\?*applicationId="$FRESH") list "" "" ;;
      *hostedDeployments\?*) list "" "$(item "$DEP_MINE")" ;;
      *hostedDeployments/*) dep_detail_mine ;;
    esac
    ;;

  create_fresh) # 何も無い状態から作る（placeholder 置換の境界確認に使う）
    case "$method$uri" in
      POST*hostedApplications) detail "$FRESH" "$HA_OWNER_TAG" ;;
      POST*hostedDeployments) dep_detail_mine ;;
      *"displayName="*) list "" "" ;;
      *hostedApplications/*) detail "$FRESH" "$HA_OWNER_TAG" ;;
      *hostedDeployments\?*) list "" "" ;;
      *hostedDeployments/*) dep_detail_mine ;;
    esac
    ;;
esac
