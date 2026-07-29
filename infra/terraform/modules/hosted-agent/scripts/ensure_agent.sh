#!/bin/sh
# PORT-03: ホスト型エージェント(Hosted Application + Deployment)を1つ用意する。
#
# なぜ Terraform の resource ではなくこのスクリプトなのかは main.tf 冒頭のコメント参照
# (provider 8.24.0 は work request の完了判定を誤り、必ず失敗する)。
#
# 入力はすべて環境変数。コマンド本文へ値を内挿しないので、利用者入力(image_tag 等)に
# `$(...)` や引用符が入っていてもシェル実行や JSON 破壊は起きない(review F-003)。
# リクエスト JSON は Terraform の jsonencode が組み立てたものをそのまま使う。シェルで
# 文字列連結すると改行・タブ・Unicode のエスケープを取りこぼす(review F-005)。
#   HA_REGION HA_COMPARTMENT HA_NAME HA_OWNER_TAG HA_CONTAINER_URI HA_TAG
#   HA_APP_BODY HA_DEP_BODY (後者は hostedApplicationId が __APP_OCID__ のプレースホルダ)
#   HA_CONFIG_FINGERPRINT (設定の指紋。再利用してよいかの判定に使う)
#
# 冪等: 自分が作った(所有者タグ付きの)ACTIVE なアプリとデプロイメントがあれば再利用する。
# 自己修復: 自分のものが FAILED 等で残っていれば、アプリごと削除してから作り直す
#           (create 時に失敗した terraform_data は tainted になり destroy provisioner が
#            走らないため、放置すると次回も同じ状態を拾って失敗し続ける — review F-004)。
# 失敗の扱い: API 呼び出しの失敗を「リソースが無い」と取り違えない。取り違えると重複作成や
#            孤児化を招くため、想定外の失敗はその場で終了する(review F-001)。
set -eu

BASE="https://generativeai.${HA_REGION}.oci.oraclecloud.com/20231130"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# GET/POST/DELETE の応答を $TMP/resp に落とす。成功時 0、失敗時は非0を返す。
# パイプ越しだと CLI の終了コードが最後のコマンドに隠れるので必ず単体で呼ぶ。
api() { oci raw-request --region "$HA_REGION" "$@" > "$TMP/resp" 2>"$TMP/err"; }

fail() { echo "$1" >&2; [ -s "$TMP/err" ] && sed -n '1,5p' "$TMP/err" >&2; exit 1; }

# 直前の api 失敗が「存在しない(404)」かどうか。401/403/429/5xx/通信断と区別する。
# 区別せず「もう無い」と扱うと、実体が残ったまま state だけ進んでしまう(review F-001/F-002)。
was_not_found() { grep -qiE '\b404\b|NotAuthorizedOrNotFound' "$TMP/err"; }

pick_ocid() { grep -oE '"id": "ocid1\.'"$1"'[^"]*"' "$TMP/resp" | head -1 | sed -E 's/.*"(ocid1[^"]*)"/\1/'; }
pick_state() { grep -oE '"lifecycleState": "[A-Z_]+"' "$TMP/resp" | head -1 | sed -E 's/.*"([A-Z_]+)"/\1/'; }
is_owned() { tr -d ' \n' < "$TMP/resp" | grep -q "\"jetuse-owner\":\"$HA_OWNER_TAG\""; }

# 同名アプリのうち DELETED 以外を1件返す(無ければ空)。API 失敗時は終了する。
# lifecycleState を列挙して問い合わせると DELETING 等を取りこぼすため、絞り込みは
# クライアント側で行う(review F-004)。
find_live_app() {
  api --http-method GET --target-uri "$BASE/hostedApplications?compartmentId=$HA_COMPARTMENT&displayName=$HA_NAME" ||
    fail "Hosted Application の一覧取得に失敗しました。権限とネットワークを確認してください"
  tr -d ' \n' < "$TMP/resp" | sed 's/},{/}\n{/g' |
    grep -v '"lifecycleState":"DELETED"' |
    grep -oE '"id":"ocid1\.generativeaihostedapplication[^"]*"' | head -1 |
    sed -E 's/.*"(ocid1[^"]*)"/\1/'
}

delete_app_and_wait() { # delete_app_and_wait <app-ocid>
  api --http-method DELETE --target-uri "$BASE/hostedApplications/$1" ||
    fail "Hosted Application $HA_NAME の削除に失敗しました"
  i=0
  while [ "$i" -lt 60 ]; do
    i=$((i + 1))
    if api --http-method GET --target-uri "$BASE/hostedApplications/$1"; then
      [ "$(pick_state)" = DELETED ] && return 0
    elif was_not_found; then
      return 0 # 404＝本当に消えた
    else
      fail "Hosted Application $HA_NAME の削除確認に失敗しました（404 以外の失敗を削除完了とみなしません）"
    fi
    sleep 12
  done
  fail "Hosted Application $HA_NAME が DELETED になりませんでした"
}

# --- 1) アプリ: 自分の ACTIVE があれば再利用、無ければ（残骸を掃除して）作る ---
FOUND="$(find_live_app)"
APP=""
if [ -n "$FOUND" ]; then
  # 所有権を確認できない障害時に新規作成へ進むと重複を作るので、ここで失敗させる(review F-003)。
  api --http-method GET --target-uri "$BASE/hostedApplications/$FOUND" ||
    fail "同名の Hosted Application が見つかりましたが取得に失敗し、所有権を確認できませんでした"
  if ! is_owned; then
    echo "同名の Hosted Application ($HA_NAME) がありますが、このスタックが作ったものではありません。" >&2
    echo "prefix を変更するか既存リソースを確認してください（既存リソースには触れません）。" >&2
    exit 1
  fi
  ST="$(pick_state)"
  # 設定が現在の Terraform 入力と一致するかは指紋タグで見る。scalingConfig や
  # inboundAuthConfig が変わっているのに再利用すると、入力変更が実環境へ届かない(review F-005)。
  if [ "$ST" = ACTIVE ] && tr -d ' \n' < "$TMP/resp" | grep -q "\"jetuse-config\":\"$HA_CONFIG_FINGERPRINT\""; then
    APP="$FOUND"
  else
    echo "作り直し: $HA_NAME は state=$ST / 設定不一致のため削除します"
    delete_app_and_wait "$FOUND"
  fi
fi

if [ -z "$APP" ]; then
  printf '%s' "$HA_APP_BODY" > "$TMP/app.json"
  api --http-method POST --target-uri "$BASE/hostedApplications" --request-body "file://$TMP/app.json" ||
    fail "Hosted Application $HA_NAME の作成に失敗しました"
  APP="$(pick_ocid generativeaihostedapplication)"
  [ -n "$APP" ] || fail "Hosted Application $HA_NAME の作成応答に OCID がありませんでした"

  i=0; ST=""
  while [ "$i" -lt 60 ]; do
    i=$((i + 1))
    api --http-method GET --target-uri "$BASE/hostedApplications/$APP" ||
      fail "Hosted Application $HA_NAME の状態取得に失敗しました"
    ST="$(pick_state)"
    [ "$ST" = ACTIVE ] && break
    case "$ST" in
      FAILED | DELETED)
        fail "Hosted Application $HA_NAME が $ST になりました（inbound の domain URL が実在するか確認してください）"
        ;;
    esac
    sleep 10
  done
  [ "$ST" = ACTIVE ] || fail "Hosted Application $HA_NAME が ACTIVE になりませんでした"
fi

# --- 2) デプロイメント（イメージ pull + 脆弱性スキャンを伴うため時間がかかる） ---
# 1アプリ=1デプロイメント。既存があっても「自分のもので・設定が一致し・壊れていない」ことを
# 確かめてから再利用する。条件を満たさなければアプリごと作り直す（デプロイメント単体は
# ACTIVE だと削除できないため、カスケード削除が唯一の回収手段 — review F-003）。
api --http-method GET --target-uri "$BASE/hostedDeployments?compartmentId=$HA_COMPARTMENT&applicationId=$APP" ||
  fail "Hosted Deployment の一覧取得に失敗しました"
DEP="$(pick_ocid generativeaihosteddeployment)"

if [ -n "$DEP" ]; then
  api --http-method GET --target-uri "$BASE/hostedDeployments/$DEP" ||
    fail "Hosted Deployment $HA_NAME の取得に失敗しました"
  DST="$(pick_state)"
  reuse=yes
  is_owned || reuse=no
  tr -d ' \n' < "$TMP/resp" | grep -q "\"containerUri\":\"$HA_CONTAINER_URI\"" || reuse=no
  tr -d ' \n' < "$TMP/resp" | grep -q "\"tag\":\"$HA_TAG\"" || reuse=no
  [ "$DST" = ACTIVE ] || reuse=no

  if [ "$reuse" = no ]; then
    echo "作り直し: $HA_NAME のデプロイメントが再利用できません（state=$DST）。アプリごと削除します"
    delete_app_and_wait "$APP"
    echo "作り直しのため終了します。次の apply で再作成されます" >&2
    exit 1
  fi
  echo "hosted agent ready (reused): $HA_NAME"
  exit 0
fi

# 置換は hostedApplicationId フィールドに限定する。本文全体を対象にすると、
# image_tag など別の入力が同じ文字列を含んだ場合に先に置換されうる(review F-007)。
printf '%s' "$HA_DEP_BODY" |
  sed "s|\"hostedApplicationId\":\"__APP_OCID__\"|\"hostedApplicationId\":\"$APP\"|" > "$TMP/dep.json"
if grep -q '__APP_OCID__' "$TMP/dep.json"; then
  fail "hostedApplicationId の差し込みに失敗しました"
fi
api --http-method POST --target-uri "$BASE/hostedDeployments" --request-body "file://$TMP/dep.json" ||
  fail "$HA_NAME の Hosted Deployment 作成に失敗しました"
DEP="$(pick_ocid generativeaihosteddeployment)"
[ -n "$DEP" ] || fail "$HA_NAME の Hosted Deployment 作成応答に OCID がありませんでした"

i=0; ST=""
while [ "$i" -lt 120 ]; do
  i=$((i + 1))
  api --http-method GET --target-uri "$BASE/hostedDeployments/$DEP" ||
    fail "$HA_NAME の Hosted Deployment 状態取得に失敗しました"
  ST="$(pick_state)"
  [ "$ST" = ACTIVE ] && break
  case "$ST" in
    FAILED | NEEDS_ATTENTION | DELETED)
      echo "$HA_NAME の Hosted Deployment が $ST になりました（イメージ $HA_CONTAINER_URI:$HA_TAG を確認してください）" >&2
      # 壊れたまま残すと次の apply でも同じものを拾うので、アプリごと消してから失敗する。
      delete_app_and_wait "$APP" || true
      exit 1
      ;;
  esac
  sleep 15
done
[ "$ST" = ACTIVE ] || fail "$HA_NAME の Hosted Deployment が30分以内に ACTIVE になりませんでした"

echo "hosted agent ready: $HA_NAME"
