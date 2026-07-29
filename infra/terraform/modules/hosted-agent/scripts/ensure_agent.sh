#!/bin/sh
# PORT-03: ホスト型エージェント(Hosted Application + Deployment)を1つ用意する。
#
# 入力はすべて環境変数。コマンド本文へ値を内挿しないので、利用者入力(image_tag 等)に
# `$(...)` や引用符が入っていてもシェル実行や JSON 破壊は起きない。
# リクエスト JSON は Terraform の jsonencode が組み立てたものをそのまま使う
# （シェルで文字列連結すると改行・タブ・Unicode のエスケープを取りこぼす）。
#   HA_REGION HA_COMPARTMENT HA_NAME HA_OWNER_TAG HA_CONTAINER_URI HA_TAG
#   HA_CONFIG_FINGERPRINT  設定の指紋。再利用してよいかの判定に使う
#   HA_APP_BODY            Hosted Application の作成本文
#   HA_DEP_BODY            Hosted Deployment の作成本文(hostedApplicationId が placeholder)
#
# 冪等: 自分が作った ACTIVE なアプリとデプロイメントがあり、設定の指紋も一致すれば再利用する。
# 自己修復: 自分のものが FAILED 等で残っていればアプリごと削除して作り直す
#           (create に失敗した terraform_data は tainted になり destroy provisioner が
#            走らないため、放置すると次回も同じ状態を拾って失敗し続ける)。
# 失敗の扱い: API 失敗を「リソースが無い」と取り違えない。取り違えると重複作成や孤児化を招く。
set -eu

. "$(dirname "$0")/lib.sh"

find_owned_app
APP="$OWNED_APP"

if [ -n "$APP" ]; then
  # find_owned_app の最後の GET 応答がこのアプリのもの。状態と設定指紋を見る。
  ST="$(pick_state)"
  if [ "$ST" = ACTIVE ] && resp_has "\"jetuse-config\":\"$HA_CONFIG_FINGERPRINT\""; then
    : # そのまま再利用する
  else
    # scalingConfig / inboundAuthConfig / 環境変数の変更が実環境へ届かない穴を塞ぐ。
    echo "作り直し: $HA_NAME は state=$ST / 設定不一致のため削除します"
    delete_app_and_wait "$APP"
    APP=""
  fi
fi

if [ -z "$APP" ]; then
  if [ "$FOREIGN_APP_EXISTS" = 1 ]; then
    echo "同名の Hosted Application ($HA_NAME) がありますが、このスタックが作ったものではありません。" >&2
    echo "prefix を変更するか既存リソースを確認してください（既存リソースには触れません）。" >&2
    exit 1
  fi

  printf '%s' "$HA_APP_BODY" > "$TMP/app.json"
  api --http-method POST --target-uri "$BASE/hostedApplications" --request-body "file://$TMP/app.json" ||
    fail "Hosted Application $HA_NAME の作成に失敗しました"
  APP="$(pick_ocid generativeaihostedapplication)"
  [ -n "$APP" ] || fail "Hosted Application $HA_NAME の作成応答に OCID がありませんでした"

  i=0
  ST=""
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

# --- デプロイメント（イメージ pull + 脆弱性スキャンを伴うため時間がかかる） ---
# 1アプリ=1デプロイメント。既存があっても「自分のもので・設定が一致し・壊れていない」ことを
# 確かめてから再利用する。条件を満たさなければアプリごと作り直す（デプロイメント単体は
# ACTIVE だと削除できず、カスケード削除が唯一の回収手段）。
api --http-method GET --target-uri "$BASE/hostedDeployments?compartmentId=$HA_COMPARTMENT&applicationId=$APP" ||
  fail "Hosted Deployment の一覧取得に失敗しました"
DEP="$(pick_ocid generativeaihosteddeployment)"

if [ -n "$DEP" ]; then
  api --http-method GET --target-uri "$BASE/hostedDeployments/$DEP" ||
    fail "Hosted Deployment $HA_NAME の取得に失敗しました"
  DST="$(pick_state)"

  # 自分のアプリの配下に、自分が作っていないデプロイメントがある状態は想定外。
  # ここでアプリごと消すと管理外リソースを巻き込むので、消さずに止める。
  if ! is_owned; then
    echo "$HA_NAME のデプロイメントがこのスタックの管理外です。手動で確認してください（自動削除はしません）" >&2
    exit 1
  fi

  reuse=yes
  resp_has "\"containerUri\":\"$HA_CONTAINER_URI\"" || reuse=no
  resp_has "\"tag\":\"$HA_TAG\"" || reuse=no
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
# image_tag など別の入力が同じ文字列を含んだ場合に先に置換されうる。
printf '%s' "$HA_DEP_BODY" |
  sed "s|\"hostedApplicationId\":\"__APP_OCID__\"|\"hostedApplicationId\":\"$APP\"|" > "$TMP/dep.json"
# 検査も当該フィールドに限定する（image_tag に同じ文字列を入れられても誤検知しない）。
if grep -q '"hostedApplicationId":"__APP_OCID__"' "$TMP/dep.json"; then
  fail "hostedApplicationId の差し込みに失敗しました"
fi

api --http-method POST --target-uri "$BASE/hostedDeployments" --request-body "file://$TMP/dep.json" ||
  fail "$HA_NAME の Hosted Deployment 作成に失敗しました"
DEP="$(pick_ocid generativeaihosteddeployment)"
[ -n "$DEP" ] || fail "$HA_NAME の Hosted Deployment 作成応答に OCID がありませんでした"

i=0
ST=""
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
