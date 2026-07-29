# PORT-03: ensure_agent.sh / delete_agent.sh の共通処理。
#
# なぜ Terraform の resource ではなく CLI なのかは main.tf 冒頭のコメント参照
# (provider 8.24.0 は work request の完了判定を誤り、必ず失敗する)。
#
# 前提の環境変数: HA_REGION HA_COMPARTMENT HA_NAME HA_OWNER_TAG

BASE="https://generativeai.${HA_REGION}.oci.oraclecloud.com/20231130"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# 応答は $TMP/resp、エラーは $TMP/err に落とす。成功時 0。
# パイプ越しに呼ぶと CLI の終了コードが最後のコマンドに隠れるので、必ず単体で呼ぶ。
api() { oci raw-request --region "$HA_REGION" "$@" > "$TMP/resp" 2>"$TMP/err"; }

fail() {
  echo "$1" >&2
  [ -s "$TMP/err" ] && sed -n '1,5p' "$TMP/err" >&2
  exit 1
}

pick_ocid() { grep -oE '"id": "ocid1\.'"$1"'[^"]*"' "$TMP/resp" | head -1 | sed -E 's/.*"(ocid1[^"]*)"/\1/'; }
pick_state() { grep -oE '"lifecycleState": "[A-Z_]+"' "$TMP/resp" | head -1 | sed -E 's/.*"([A-Z_]+)"/\1/'; }
resp_has() { tr -d ' \n' < "$TMP/resp" | grep -q "$1"; }
is_owned() { resp_has "\"jetuse-owner\":\"$HA_OWNER_TAG\""; }

# 同名アプリのうち DELETED 以外の OCID を $TMP/apps へ書く。API 失敗時は非0を返す。
# lifecycleState を列挙して問い合わせると DELETING 等を取りこぼすので、
# 絞り込みはクライアント側で行う。
#
# **コマンド置換で呼ばないこと**。$(...) の中で fail を呼んでもサブシェルが終わるだけで、
# 呼び出し元は「該当なし」として続行してしまう（実際にモックで踏んだ）。
list_live_apps() {
  api --http-method GET --target-uri "$BASE/hostedApplications?compartmentId=$HA_COMPARTMENT&displayName=$HA_NAME" ||
    return 1
  tr -d ' \n' < "$TMP/resp" | sed 's/},{/}\n{/g' |
    grep -v '"lifecycleState":"DELETED"' |
    grep -oE '"id":"ocid1\.generativeaihostedapplication[^"]*"' |
    sed -E 's/.*"(ocid1[^"]*)"/\1/' > "$TMP/apps"
}

# 同名候補のうち **このスタックが作った** ものを探す。
# 同名の管理外アプリが混ざっていても、それを掴まない・消さない。
# 候補ごとの詳細取得に失敗したら、所有権を確認できないので終了する。
#
# 結果はグローバルへ書く（コマンド置換で呼ぶとサブシェルになり、
# FOREIGN_APP_EXISTS の更新が呼び出し元へ伝わらないため）:
#   OWNED_APP           … 自分のアプリの OCID（無ければ空）
#   FOREIGN_APP_EXISTS  … 同名の管理外アプリがあれば 1
# 成功時、$TMP/resp には OWNED_APP の詳細が残る（状態・タグの判定に使う）。
find_owned_app() {
  OWNED_APP=""
  FOREIGN_APP_EXISTS=0
  list_live_apps ||
    fail "Hosted Application の一覧取得に失敗しました。権限とネットワークを確認してください"
  # while ... done < file は現在のシェルで走る（パイプにするとサブシェルになる）。
  while read -r _id; do
    [ -n "$_id" ] || continue
    api --http-method GET --target-uri "$BASE/hostedApplications/$_id" ||
      fail "Hosted Application $_id の取得に失敗し、所有権を確認できませんでした"
    if is_owned; then
      OWNED_APP="$_id"
      return 0
    fi
    FOREIGN_APP_EXISTS=1
  done < "$TMP/apps"
  return 0
}

# OCI は「存在しない」も「権限が無い」も 404 + NotAuthorizedOrNotFound で返すため、
# 404 だけを見て削除完了と判断できない。一覧に出てこないことで確かめる
# （一覧自体が失敗するなら判断せず失敗させる）。
app_gone() { # app_gone <app-ocid>
  api --http-method GET --target-uri "$BASE/hostedApplications?compartmentId=$HA_COMPARTMENT&displayName=$HA_NAME" || return 1
  ! resp_has "\"id\":\"$1\""
}

# アプリを削除し、DELETED を確認するまで待つ（デプロイメントはカスケード削除される）。
delete_app_and_wait() { # delete_app_and_wait <app-ocid>
  api --http-method DELETE --target-uri "$BASE/hostedApplications/$1" ||
    fail "Hosted Application $HA_NAME の削除要求に失敗しました"
  _i=0
  while [ "$_i" -lt 60 ]; do
    _i=$((_i + 1))
    if api --http-method GET --target-uri "$BASE/hostedApplications/$1"; then
      [ "$(pick_state)" = DELETED ] && return 0
    elif app_gone "$1"; then
      return 0
    else
      fail "Hosted Application $HA_NAME の削除確認に失敗しました（権限失効と不存在を区別できないため中断します）"
    fi
    sleep 12
  done
  fail "Hosted Application $HA_NAME が DELETED になりませんでした"
}
