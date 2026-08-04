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

# 一覧 API を **最後のページまで** 辿り、DELETED 以外の OCID を出力ファイルへ書く。
# API 失敗時は非0を返す。lifecycleState を列挙して問い合わせると DELETING 等を
# 取りこぼすので、絞り込みはクライアント側で行う。
#
# ページングを省くと「1ページ目に無い＝存在しない」と誤判断する（削除確認が特に危ない）。
# `oci raw-request` は応答ヘッダも出力に含めるので、opc-next-page はそこから拾える。
#
# **コマンド置換で呼ばないこと**。$(...) の中で fail を呼んでもサブシェルが終わるだけで、
# 呼び出し元は「該当なし」として続行してしまう（実際にモックで踏んだ）。
list_live_ids() { # list_live_ids <list-uri> <ocid-type> <out-file>
  : > "$3"
  _page=""
  _pages=0
  while :; do
    _pages=$((_pages + 1))
    if [ -n "$_page" ]; then
      api --http-method GET --target-uri "$1&page=$_page" || return 1
    else
      api --http-method GET --target-uri "$1" || return 1
    fi
    tr -d ' \n' < "$TMP/resp" | sed 's/},{/}\n{/g' |
      grep -v '"lifecycleState":"DELETED"' |
      grep -oE "\"id\":\"ocid1\.$2[^\"]*\"" |
      sed -E 's/.*"(ocid1[^"]*)"/\1/' >> "$3"
    _page="$(tr -d ' ' < "$TMP/resp" | grep -oiE '"opc-next-page":"[^"]*"' | head -1 | sed -E 's/.*:"([^"]*)"/\1/')"
    [ -n "$_page" ] || return 0
    [ "$_pages" -lt 50 ] && continue
    fail "一覧のページングが 50 ページを超えました（$HA_NAME の候補が想定外に多い）"
  done
}

list_live_apps() {
  list_live_ids "$BASE/hostedApplications?compartmentId=$HA_COMPARTMENT&displayName=$HA_NAME" \
    generativeaihostedapplication "$TMP/apps"
}

# 同名候補のうち **このスタックが作った** ものを探す。
# 同名の管理外アプリが混ざっていても、それを掴まない・消さない。
# 候補ごとの詳細取得に失敗したら、所有権を確認できないので終了する。
#
# 候補は **最後まで** 見る。1件目で打ち切ると、所有アプリが複数残っている状態で
# 先頭だけ消して destroy が成功し、残りが state 外の孤児になる。
# 所有アプリが複数あるのは自動処理の想定外なので、副作用を出さずに止める。
#
# 結果はグローバルへ書く（コマンド置換で呼ぶとサブシェルになり、
# FOREIGN_APP_EXISTS の更新が呼び出し元へ伝わらないため）:
#   OWNED_APP           … 自分のアプリの OCID（無ければ空）
#   FOREIGN_APP_EXISTS  … 同名の管理外アプリがあれば 1
# 成功時、$TMP/resp には OWNED_APP の詳細が残る（状態・タグの判定に使う）。
find_owned_app() {
  OWNED_APP=""
  FOREIGN_APP_EXISTS=0
  _owned=0
  list_live_apps ||
    fail "Hosted Application の一覧取得に失敗しました。権限とネットワークを確認してください"
  # while ... done < file は現在のシェルで走る（パイプにするとサブシェルになる）。
  while read -r _id; do
    [ -n "$_id" ] || continue
    api --http-method GET --target-uri "$BASE/hostedApplications/$_id" ||
      fail "Hosted Application $_id の取得に失敗し、所有権を確認できませんでした"
    if is_owned; then
      _owned=$((_owned + 1))
      OWNED_APP="$_id"
      cp "$TMP/resp" "$TMP/owned_app"
    else
      FOREIGN_APP_EXISTS=1
    fi
  done < "$TMP/apps"
  [ "$_owned" -le 1 ] ||
    fail "同名 ($HA_NAME) でこのスタック所有の Hosted Application が ${_owned} 件あります。どれが正か判断できないため中断します（手動で整理してください）"
  if [ -n "$OWNED_APP" ]; then
    cp "$TMP/owned_app" "$TMP/resp"
  fi
  return 0
}

# アプリ配下のデプロイメントを **全件** 調べ、所有権を確定する。
# 結果はグローバルへ書く:
#   OWNED_DEP           … 自分のデプロイメントの OCID（無ければ空）
#   FOREIGN_DEP_EXISTS  … 管理外のデプロイメントがあれば 1
# 所有デプロイメントがある場合、$TMP/resp にはその詳細が残る。
scan_deployments() { # scan_deployments <app-ocid>
  OWNED_DEP=""
  FOREIGN_DEP_EXISTS=0
  _owned_dep=0
  list_live_ids "$BASE/hostedDeployments?compartmentId=$HA_COMPARTMENT&applicationId=$1" \
    generativeaihosteddeployment "$TMP/deps" ||
    fail "Hosted Deployment の一覧取得に失敗しました"
  while read -r _did; do
    [ -n "$_did" ] || continue
    api --http-method GET --target-uri "$BASE/hostedDeployments/$_did" ||
      fail "Hosted Deployment $_did の取得に失敗し、所有権を確認できませんでした"
    if is_owned; then
      _owned_dep=$((_owned_dep + 1))
      OWNED_DEP="$_did"
      cp "$TMP/resp" "$TMP/owned_dep"
    else
      FOREIGN_DEP_EXISTS=1
    fi
  done < "$TMP/deps"
  [ "$_owned_dep" -le 1 ] ||
    fail "$HA_NAME のデプロイメントがこのスタック所有で ${_owned_dep} 件あります（1アプリ=1デプロイメントの前提から外れています）。中断します"
  if [ -n "$OWNED_DEP" ]; then
    cp "$TMP/owned_dep" "$TMP/resp"
  fi
  return 0
}

# OCI は「存在しない」も「権限が無い」も 404 + NotAuthorizedOrNotFound で返すため、
# 404 だけを見て削除完了と判断できない。一覧に出てこないことで確かめる
# （一覧自体が失敗するなら判断せず失敗させる）。
app_gone() { # app_gone <app-ocid>
  list_live_apps || return 1
  ! grep -Fxq "$1" "$TMP/apps"
}

# アプリを削除し、DELETED を確認するまで待つ（デプロイメントはカスケード削除される）。
#
# **カスケードで管理外を巻き込まないこと**が削除の前提条件。アプリ配下に管理外の
# デプロイメントが1件でもあれば、副作用を出さずに止めて人間の判断に回す
# （既存リソースの変更は人間ゲート — CLAUDE.md）。この検査は削除の入口に置く。
# 呼び出し側の経路（destroy / 設定不一致の作り直し / 壊れたデプロイメントの回収）が
# 増えても検査を通らない経路が生まれないようにするため。
delete_app_and_wait() { # delete_app_and_wait <app-ocid>
  scan_deployments "$1"
  if [ "$FOREIGN_DEP_EXISTS" = 1 ]; then
    fail "$HA_NAME の配下にこのスタック管理外の Hosted Deployment があります。アプリを削除すると巻き込むため中断します（手動で確認してください）"
  fi
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
