#!/usr/bin/env bash
# 共有基盤(environments/dev の構成)を OCI Resource Manager のスタックとして扱う（ADR-0031）。
#
# **なぜ ORM か**: ローカル state はこの1台にしか無く、失うと資源が管理不能な孤児になる。
# ORM なら state を OCI が持ち、ジョブ履歴が残り、インスタンス dev からも CI からも動かせる。
#
# 使い方:
#   ops/orm-stack.sh <env> plan            # plan ジョブ（安全・いつでも可）
#   ops/orm-stack.sh <env> apply --apply   # apply ジョブ（**--apply 明示が必須**）
#   ops/orm-stack.sh <env> import          # 既存ローカル state を取り込む plan（移行時のみ）
#   ops/orm-stack.sh <env> import --apply  # 取り込みを実行
#   ops/orm-stack.sh <env> state > out.tfstate   # ORM の state を落とす
#
# <env> は internal-dev | public-dev（下の case で定義）。コンパートメント OCID は .env から
# 解決し、**リポジトリに実 OCID を置かない**。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ENV_NAME="${1:-}"
ACTION="${2:-plan}"
APPLY_FLAG="${3:-}"

usage() { sed -n '2,20p' "$0"; exit 2; }
[ -n "$ENV_NAME" ] || usage
# **action は最初に検証する。** 後ろの case まで持ち越すと、打ち間違い1つで
# 「usage で失敗する前に既存スタックの構成と変数を更新してしまう」（2026-08-09 のレビュー指摘）。
case "$ACTION" in
  plan|apply|import|state) ;;
  *) echo "未知の action: $ACTION （plan | apply | import | state）" >&2; exit 2 ;;
esac

# --- 環境ごとの設定（実 OCID は .env 側。ここには名前だけ） -------------------
# IAM(動的グループ・ランタイムポリシー)を**この構成で作るか**は環境ごとに違う。
# 既定は両方 false なので、渡さないと**黙って IAM 抜きの環境ができる**
# (2026-08-08 実測: public-dev の初回 plan が 32 資源で、module.iam が 0 件だった。
#  新規コンパートメントでこれをやると、リソースプリンシパルに権限が無く
#  Container Instance も Functions も ADB を触れない)。
# **動的グループはこの構成では作らず、環境ごとに1本を外で用意して参照する。**
# 理由が2つある。
#  1. テナンシの DynamicResourceGroups に上限があり、2026-08-08 時点で 50 本埋まっていた
#     (うち 48 本は他プロジェクトのもので消せない)。環境ごとに 3 本ずつは作れない。
#  2. **1本にまとめても権限は広がらない。** `enable_dynamic_group=false` にすると
#     runtime / adb / semantic_store の3参照がすべて `existing_dynamic_group` に畳まれる。
#
# **環境をまたいで共用してはいけない。** ポリシーは「動的グループ全体」に権限を与えるので、
# 複数コンパートメントを含む DG を使うと、internal-dev の Container Instance が public-dev の
# ADB やバケットに届く。5面構成の権限境界が成立しなくなる(Codex review-1 の blocker)。
# 各 DG の matching rule は**その環境のコンパートメントだけ**を指すこと。
#
# 名前は下の case で環境ごとに持つ。**連想配列(`declare -A`)は使わない** ——
# macOS 既定の bash 3.2 に無く、`internal: unbound variable` で落ちる(2026-08-08 実測)。

case "$ENV_NAME" in
  internal-dev)
    COMPARTMENT_VAR=INTERNAL_DEV_COMPARTMENT_OCID
    PREFIX=jetuse-dev            # 既存資源の命名。**変えると全部作り直しになる**
    REGION_DEFAULT=us-chicago-1
    # 既存の IAM(動的グループもポリシーも)をそのまま使う。この構成では何も作らない。
    ENABLE_DG=false
    ENABLE_POLICY=false
    DG_NAME=jetuse-internal-dg
    ;;
  public-dev)
    COMPARTMENT_VAR=PUBLIC_DEV_COMPARTMENT_OCID
    PREFIX=jetuse-pubdev
    REGION_DEFAULT=us-chicago-1
    # 動的グループは public-dev だけを指す `jetuse-pubdev-dg` を参照する。
    ENABLE_DG=false
    ENABLE_POLICY=true
    DG_NAME=jetuse-pubdev-dg
    ;;
  *) echo "未知の env: $ENV_NAME （internal-dev | public-dev）" >&2; exit 2 ;;
esac

# .env から読む（コミットしない値の単一の置き場）
[ -f .env ] && set -a && . ./.env && set +a
# **環境をまたぐ override を用意しない。** 以前は `JETUSE_SHARED_DYNAMIC_GROUP` で
# どの環境の DG 名も差し替えられた。public-dev に `jetuse-internal-dg` を指せば、
# internal-dev の principal に public-dev の 20 文が付き、閉じたはずの境界がまた開く。
# 名前は下の case が持つ値だけを使う。
SHARED_DG="${DG_NAME:-}"
: "${SHARED_DG:?${ENV_NAME} の動的グループ名が決まっていない}"
COMPARTMENT="$(eval "echo \${$COMPARTMENT_VAR:-}")"
: "${COMPARTMENT:?$COMPARTMENT_VAR が .env に無い}"
: "${TENANCY_OCID:?TENANCY_OCID が .env に無い}"
# **スタックの所在**と**資源を作る先**は別物。同じ変数にすると、配備先を変えた瞬間に
# 既存スタックを別リージョンで探して見失い、同名をもう1本作る。
STACK_REGION="${ORM_STACK_REGION:-$REGION_DEFAULT}"   # ORM スタックが住むリージョン
REGION="${ORM_REGION:-$REGION_DEFAULT}"               # terraform が資源を作るリージョン
STACK_NAME="jetuse-${ENV_NAME}-foundation"
SRC=infra/terraform/environments/dev

# **CLI の失敗を「スタックが無い」に潰さない。** 認証切れや通信断で検索に失敗したときに
# 空を返すと、同名スタックをもう1本作り、apply では**空の state から既存資源を作り直そうと
# する**。0 件と失敗は別物として扱う。同名が複数あるのも異常なので止める。
find_stack() {
  local out
  if ! out=$(oci resource-manager stack list --compartment-id "$COMPARTMENT" --region "$STACK_REGION" \
              --display-name "$STACK_NAME" --query 'data[].id' --output json 2>&1); then
    echo "スタック検索に失敗した（認証・通信を確認）: $out" >&2
    return 1
  fi
  python3 - "$out" <<'PYFIND'
import json, sys
# **解析できないものを「0 件」と読まない。** 警告混入や出力仕様の変化を
# 「スタックが無い」と誤認すると、同名スタックを作って空 state から資源を作り直す。
try:
    ids = json.loads(sys.argv[1])
except Exception as e:
    sys.exit(f"スタック一覧を解釈できない: {e}")
if ids is None:
    ids = []
if not isinstance(ids, list):
    sys.exit("スタック一覧の形が想定外")
if len(ids) > 1:
    sys.exit(f"同名スタックが {len(ids)} 本ある。手で整理すること")
print(ids[0] if ids else "")
PYFIND
}

# --- state は最短経路で返す ----------------------------------------------------
# **stdout に state 以外を出さない。** 進捗メッセージが混ざると、受け側
# (ops/dev-env-up.sh) の JSON パースが「Expecting value: line 1 column 1」で落ちる
# (2026-08-08 に実際に踏んだ)。config の zip 化も要らないので、ここで打ち切る。
if [ "$ACTION" = state ]; then
  sid="$(find_stack)"
  [ -n "$sid" ] && [ "$sid" != null ] || { echo "スタックが無い: $STACK_NAME" >&2; exit 1; }
  exec oci resource-manager stack get-stack-tf-state --stack-id "$sid" --region "$STACK_REGION" --file -
fi

# ここから先はスタックを作る／動かす経路。**`state`（読み取り）はここへ来ない**ので、
# ADB の管理パスワードを要求しない（取得に不要な秘密を実行環境に強いない）。
: "${ADB_ADMIN_PASSWORD:?ADB_ADMIN_PASSWORD が .env に無い}"

# --- プリフライト: ADB が止まっていないか --------------------------------------
# **止まった ADB には apply できない。** admin_password の更新が
# 409 IncorrectState で弾かれ、**ジョブの後半まで走ってから**落ちる
# (2026-08-08 に実際に踏んだ)。数分待たされた挙句に部分適用が残るので、投げる前に見る。
# 共有 ADB は夜間停止して自動再開しない(backlog #10)。
# **対象を display name で絞る。** `data[0]` だけ見ると、同じコンパートメントに別の ADB が
# あったときに**関係ない方の状態で判断**して、対象の STOPPED を見逃す。
# CLI の失敗も握り潰さない（握り潰すと「確認したつもり」で apply に進む）。
if [ "$ACTION" = apply ] || [ "$ACTION" = import ]; then
  ADB_NAME="${PREFIX}-adb"
  if ! adb_json=$(oci db autonomous-database list -c "$COMPARTMENT" --region "$REGION" \
        --query "data[?\"display-name\"=='${ADB_NAME}'].\"lifecycle-state\"" --output json 2>&1); then
    echo "!! ADB の状態を確認できない（認証・通信を確認）: $adb_json" >&2
    exit 1
  fi
  adb_state=$(python3 -c '
import json, sys
s = json.loads(sys.argv[1]) or []
if len(s) > 1:
    sys.exit(f"同名 ADB が {len(s)} 台ある: {sys.argv[2]}")
print(s[0] if s else "")
' "$adb_json" "$ADB_NAME") || exit 1
  if [ -z "$adb_state" ]; then
    # まだ ADB が無い（新規構築の初回）のは正常。作ってから止まる話ではない。
    echo "   ${ADB_NAME} は未作成（初回構築とみなして続行）"
  elif [ "$adb_state" != "AVAILABLE" ]; then
    echo "!! ${ADB_NAME} (${REGION}) が ${adb_state}。apply は 409 IncorrectState で落ちる。" >&2
    echo "   先に起動する: ops/start-adb-if-stopped.sh ${ENV_NAME}" >&2
    exit 1
  fi
fi

# --- 先に決めておく（import の前提検査がこれらを使う） ------------------------
STACK_ID="$(find_stack)"
# **「未設定」と「空を明示」を区別する。** 未設定ならスタックの現行値を守り、
# 空を明示したときは消す（既定＝apply 時刻起点 +1年 に戻す）。`${VAR:-}` では
# 両者が同じ空文字になり、一度入った値を消す手段が無くなる。
if [ "${SPA_PAR_EXPIRY+x}" = x ]; then PAR_SET=1; else PAR_SET=0; fi

# --- 構成 zip を作る ----------------------------------------------------------
# ORM は zip の中身をそのまま terraform に食わせる。modules/ は相対パス ../../modules を
# 見るので、同じ階層構造で詰める。
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT   # VARS_FILE 確保後に張り替える
mkdir -p "$WORK/environments/dev" "$WORK/modules"
cp "$SRC"/*.tf "$WORK/environments/dev/"
# **`.terraform` を持ち込まない。** 素朴に cp -R すると、モジュール配下のプロバイダキャッシュ
# (2026-08-08 実測: iam と hosted-agent に 251MB ずつ) まで詰めて zip が 128MB になり、
# ORM が 413 RequestEntityTooLarge で受け取らない。実体は 328KB / 64 ファイル。
# lock ファイル(.terraform.lock.hcl)は**残す** —— プロバイダ版を固定して再現性を保つため。
tar -cf - -C infra/terraform/modules \
  --exclude='.terraform' --exclude='*.tfstate' --exclude='*.tfstate.*' . \
  | tar -xf - -C "$WORK/modules"
# schema.yaml はスタック変数の型定義（password をマスクするために要る）
[ -f "$SRC/schema.yaml" ] && cp "$SRC/schema.yaml" "$WORK/"

if [ "$ACTION" = "import" ]; then
  STATE="$SRC/terraform.tfstate"
  [ -f "$STATE" ] || { echo "ローカル state が無い: $STATE" >&2; exit 1; }

  # **一度きりの移行操作。二度目を許さない。** 既に ORM 側が資源を持っているのに
  # ローカル state から import すると、同じ実資源を2つの state が所有することになる。
  if [ -n "$STACK_ID" ] && [ "$STACK_ID" != "null" ]; then
    # **パイプの後ろに `|| echo -1` を付けない。** `pipefail` 下では前段が失敗しても
    # 後段の python が 0 を出し、そのうえ `-1` も足されて `n_orm` が "0\n-1" になる。
    # 続く整数比較が壊れて**検査そのものが無効化される**（2026-08-09 のレビュー指摘）。
    # 取得と解釈を分ける。
    orm_state_file="$(mktemp)"
    if ! oci resource-manager stack get-stack-tf-state --stack-id "$STACK_ID" \
           --region "$STACK_REGION" --file "$orm_state_file" 2>/dev/null; then
      rm -f "$orm_state_file"
      echo "!! ORM の state を取得できない。移行済みか判断できないので止める。" >&2
      exit 1
    fi
    n_orm=$(python3 -c '
import json, sys
raw = open(sys.argv[1]).read().strip()
if not raw:
    print(0); raise SystemExit
d = json.loads(raw)
print(sum(len(r.get("instances", [])) for r in d.get("resources", [])))
' "$orm_state_file") || { rm -f "$orm_state_file"; echo "!! ORM state を解釈できない" >&2; exit 1; }
    rm -f "$orm_state_file"
    if [ "$n_orm" -ne 0 ] && [ "${ORM_FORCE_IMPORT:-}" != "1" ]; then
      echo "!! ORM の state に既に ${n_orm} 資源ある。移行は済んでいる。" >&2
      echo "   再 import は同じ資源を二重所有させる。通常は plan / apply を使うこと。" >&2
      echo "   **途中で失敗して一部だけ取り込まれた場合**は回復のため再実行が要る。" >&2
      echo "   その場合だけ ORM_FORCE_IMPORT=1 を明示する（import ブロックは取り込み済みを" >&2
      echo "   黙って飛ばすので、残りだけが取り込まれる）。" >&2
      exit 1
    fi
    [ "${ORM_FORCE_IMPORT:-}" = "1" ] && [ "$n_orm" -ne 0 ] \
      && echo "   ORM_FORCE_IMPORT=1: 既存 ${n_orm} 資源のうち未取り込み分だけを取り込む"
  fi

  # **`time_offset` を import しないので、失効日を明示しないと PAR が作り直しになる。**
  # 黙って作り直すと SPA の PAR URL が変わり、API Gateway の配線もやり直しになる。
  if [ "$PAR_SET" != "1" ]; then
    cur=$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
for r in d.get("resources", []):
    if r["type"] == "oci_objectstorage_preauthrequest":
        for i in r.get("instances", []):
            print(i.get("attributes", {}).get("time_expires", "")); raise SystemExit
print("")
' "$STATE")
    echo "!! SPA_PAR_EXPIRY が未設定。import では time_offset を取り込まないので、" >&2
    echo "   このままだと PAR が作り直しになる（SPA の URL が変わる）。" >&2
    [ -n "$cur" ] && echo "   現行の失効日: SPA_PAR_EXPIRY=${cur}" >&2
    echo "   据え置くならその値を、作り直してよいなら SPA_PAR_EXPIRY= を明示すること。" >&2
    exit 1
  fi
  # **変数展開の直後に全角文字を置かない。** ドル記号+変数名のあとに全角括弧を続けると、
  # zsh や locale 次第でその先頭バイトまで変数名として読まれ "unbound variable" で落ちる
  # (2026-08-07 に実際に踏んだ)。必ず波括弧で名前を区切る。
  echo "== import ブロックを生成: ${STATE}"
  python3 ops/orm-import-blocks.py "$STATE" > "$WORK/environments/dev/imports.tf"
  echo "   **取り込み後は imports.tf 抜きで plan し直し、No changes を確認すること**"
fi

ZIP="$WORK/config.zip"
( cd "$WORK" && zip -qr "$ZIP" environments modules $( [ -f schema.yaml ] && echo schema.yaml ) )
ZIP_KB=$(( $(wc -c < "$ZIP") / 1024 ))
echo "== 構成 zip: ${ZIP_KB}KB / $(unzip -l "$ZIP" | tail -1 | awk '{print $2}') ファイル"
# **膨れたら送る前に止める。** ORM は大きすぎる config を 413 で弾くが、そのときには
# 数分待たされた後で、原因(何が混ざったか)も分からない。手元で気づけるようにする。
MAX_KB="${ORM_ZIP_MAX_KB:-4096}"
if [ "$ZIP_KB" -gt "$MAX_KB" ]; then
  echo "!! zip が ${ZIP_KB}KB（上限 ${MAX_KB}KB）。余計なものが混ざっている。" >&2
  echo "   中身の大きい順:" >&2
  unzip -l "$ZIP" | sort -rn | head -10 >&2
  exit 1
fi

# --- スタックを作る／更新する -------------------------------------------------
# **既存のスタック変数に上書きする。作り直さない。**
# 毎回 env から組み直すと、env を1つ付け忘れただけでその変数がスタックから消える。
# 2026-08-08 に実際に踏んだ: `SPA_PAR_EXPIRY` を渡さずに plan したら spa_par_expiry が
# 消え、PAR が must be replaced に戻った（tfvars で警戒した取り違えが別の形で再発した）。
# ここで渡すのは「この実行で明示された値」だけにし、残りは ORM 側の現行値を引き継ぐ。
# **取得できないなら進まない。** 失敗を `{}` に潰すと、既存の変数
# (`enable_opensearch` / `enable_identity_domain` / `api_image_url` / `functions_routes` 等)が
# 丸ごと消えて既定値へ戻る。その状態で `apply --apply` すると AUTO_APPROVED のまま
# **稼働中の資源を削除・作り直しに行く**。
EXISTING="{}"
if [ -n "$STACK_ID" ] && [ "$STACK_ID" != "null" ]; then
  if ! EXISTING=$(oci resource-manager stack get --stack-id "$STACK_ID" --region "$STACK_REGION" \
        --query 'data.variables' --output json 2>&1); then
    echo "既存スタックの変数を取得できない（認証・通信を確認）: $EXISTING" >&2
    exit 1
  fi
fi
# **秘密を argv に載せない。** `ps` で同一ホストから読める。環境変数で渡し、
# 出力は 0600 の一時ファイルへ。CLI へは `file://` で渡す。
VARS_FILE="$(mktemp -t jetuse-orm-vars-XXXXXX)"
chmod 600 "$VARS_FILE"
trap 'rm -f "$VARS_FILE"; rm -rf "$WORK"' EXIT

JV_EXISTING="$EXISTING" JV_C="$COMPARTMENT" JV_T="$TENANCY_OCID" JV_P="$PREFIX" \
JV_PW="$ADB_ADMIN_PASSWORD" JV_DG="$ENABLE_DG" JV_POL="$ENABLE_POLICY" \
JV_SHARED="$SHARED_DG" JV_REGION="$REGION" JV_PARSET="$PAR_SET" \
JV_PAR="${SPA_PAR_EXPIRY:-}" python3 > "$VARS_FILE" <<'PY'
import json, os, sys
g = os.environ.get
existing = g("JV_EXISTING", "{}")
c, t, p, pw = g("JV_C", ""), g("JV_T", ""), g("JV_P", ""), g("JV_PW", "")
dg, pol, shared_dg = g("JV_DG", ""), g("JV_POL", ""), g("JV_SHARED", "")
region, par_set, par = g("JV_REGION", ""), g("JV_PARSET", "0"), g("JV_PAR", "")
# **解釈できないなら止める。** 空 dict に潰すと既存変数が消える。
try:
    v = json.loads(existing)
except Exception as e:
    sys.exit(f"既存スタックの変数を解釈できない: {e}")
if v is None:
    v = {}
if not isinstance(v, dict):
    sys.exit("スタック変数が dict ではない")
# 環境の設計として決まっている値。**毎回明示する**（既定に落ちると IAM 抜きの環境ができる）。
v.update({
    "compartment_ocid": c,
    "tenancy_ocid": t,
    "prefix": p,
    "adb_admin_password": pw,
    "region": region,
    "enable_dynamic_group": dg,
    "enable_runtime_policy": pol,
    # enable_dynamic_group=false のとき、ポリシーが参照する既存 DG 名。
    # **渡し忘れるとポリシーだけできて権限が付かない**（空の group を指す）。
    "existing_dynamic_group": "" if dg == "true" else shared_dg,
})
# **秘密を含みうる map はスタックに持たせない。** `api_environment` は任意のキーを取る map で、
# 値に ADB のパスワード等が入りうるが、ORM の schema は map の一部だけをマスクできない
# （コンソールで平文表示になる）。この基盤スタックは `api_image_url` を設定しないので
# Container Instance を作らず、`api_environment` を使わない。引き継がずに落とす。
# アプリ層（environments/app）が必要とするので、そちらで扱う。
# ただし **Container Instance / Function が実在するときは落とさない**。落とすと
# 環境変数が消えて稼働中のアプリが壊れる（`api_image_url` / `fn_router_image` は
# 引き継ぐので、片方だけ消すと不整合になる）。
if v.get("api_image_url") or v.get("fn_router_image"):
    if "api_environment" in v:
        print("!! api_environment を引き継ぐ（イメージが設定されているため）。"
              "ORM のコンソールで平文表示になる点に注意。", file=sys.stderr)
else:
    v.pop("api_environment", None)

# PAR の失効日は**スタックに持たせた値を引き継ぐ**。env が設定されたときだけ上書きする
# （付け忘れで消えると PAR が作り直しになる。空を明示したときは意図的な消去）。
if par_set == "1":
    v["spa_par_expiry"] = par
v.setdefault("spa_par_expiry", "")
print(json.dumps(v))
PY
echo "   変数: $(python3 -c "import json,sys;print(', '.join(sorted(json.load(open(sys.argv[1])))))" "$VARS_FILE")"

if [ -z "$STACK_ID" ] || [ "$STACK_ID" = "null" ]; then
  echo "== スタックを新規作成: $STACK_NAME"
  STACK_ID=$(oci resource-manager stack create --compartment-id "$COMPARTMENT" --region "$STACK_REGION" \
    --config-source "$ZIP" --display-name "$STACK_NAME" \
    --description "共有基盤 ($ENV_NAME)。ADR-0031" \
    --working-directory environments/dev \
    --terraform-version "${ORM_TF_VERSION:-1.5.x}" \
    --variables "file://$VARS_FILE" --wait-for-state ACTIVE \
    --query 'data.id' --raw-output)
else
  echo "== 既存スタックの構成を更新: ${STACK_ID: -12}"
  oci resource-manager stack update --stack-id "$STACK_ID" --region "$STACK_REGION" \
    --config-source "$ZIP" --working-directory environments/dev \
    --variables "file://$VARS_FILE" --force >/dev/null
fi
echo "   stack: ...${STACK_ID: -12}"

# --- ジョブ -------------------------------------------------------------------
LAST_PLAN_JOB=""
LAST_DESTROY_COUNT=0

run_job() {
  local kind="$1" jid
  if [ "$kind" = apply ]; then
    # **確認した plan を適用する。** `AUTO_APPROVED` は「その場で plan を作り直して即適用」
    # なので、直前に人が読んだ内容と食い違いうる（構成・変数・実資源が動いていれば別物になる）。
    # 直前の plan ジョブがあればそれを適用対象に固定する。
    if [ -n "$LAST_PLAN_JOB" ]; then
      jid=$(oci resource-manager job create-apply-job --stack-id "$STACK_ID" --region "$STACK_REGION" \
        --execution-plan-strategy FROM_PLAN_JOB_ID --execution-plan-job-id "$LAST_PLAN_JOB" \
        --query 'data.id' --raw-output)
      echo "   （確認済み plan ...${LAST_PLAN_JOB: -12} を適用）"
    else
      echo "!! 直前の plan が無い。AUTO_APPROVED は使わない。" >&2
      return 1
    fi
  else
    jid=$(oci resource-manager job create-plan-job --stack-id "$STACK_ID" --region "$STACK_REGION" \
      --query 'data.id' --raw-output)
    LAST_PLAN_JOB="$jid"
  fi
  echo "== ${kind} ジョブ ...${jid: -12}"
  while :; do
    st=$(oci resource-manager job get --job-id "$jid" --region "$STACK_REGION" \
      --query 'data."lifecycle-state"' --raw-output)
    case "$st" in
      SUCCEEDED|FAILED|CANCELED) break ;;
    esac
    sleep 15
  done
  echo "   → $st"

  # **`--all` が要る。** 既定はページングで打ち切られ、plan 出力は資源1つの詳細で数百行に
  # なるため、肝心の "Plan:" 行が最後まで届かない(2026-08-08 に実際に見落とした)。
  local log="${ORM_LOG_DIR:-/tmp}/orm-${ENV_NAME}-${kind}-${jid: -12}.log"
  # **0600 で作ってから書く。** plan/apply のログには PAR の `access_uri`（期限内なら
  # 認証情報として使える）や namespace が載る。素のリダイレクトだと umask 任せで、
  # 一般的な 022 では 0644 になり同一ホストの他ユーザーから読める。
  : > "$log"
  chmod 600 "$log"
  # **ログを取れなかったら判定できない。** 握り潰すと destroy 件数が 0 に見え、
  # 資源が消える plan でも `--apply` のゲートを素通りする(2026-08-09 のレビュー指摘)。
  if ! oci resource-manager job get-job-logs --job-id "$jid" --region "$STACK_REGION" --all \
        --query 'data[].message' --raw-output > "$log" 2>/dev/null; then
    echo "!! ジョブログを取得できない（判定できないので止める）: $jid" >&2
    return 1
  fi
  echo "   ログ全文: $log ($(wc -l < "$log" | tr -d ' ') 行)"
  # 要約行が1つも無いログは、途中で切れたか形式が変わったかのどちらか。信用しない。
  if ! grep -qE "Plan: [0-9]|No changes|Apply complete|Destroy complete|Error" "$log"; then
    echo "!! ログに判定行が無い（内容を信用できないので止める）: $log" >&2
    return 1
  fi

  # 判定に効く行だけを引き上げる。tail で流すと読まれない。
  echo
  echo "--- 判定 ---"
  grep -E "Plan: [0-9]|No changes|Apply complete" "$log" | sed 's/^ *"//;s/",$//' | tail -3 \
    || echo "  (要約行が見つからない。ログ全文を見ること)"
  local n_destroy
  n_destroy=$(grep -icE "will be destroyed|must be replaced" "$log" || true)
  [ "$kind" = plan ] && LAST_DESTROY_COUNT="$n_destroy"
  echo "--- destroy / replace: ${n_destroy} 件 ---"
  if [ "$n_destroy" -gt 0 ]; then
    grep -iE "will be destroyed|must be replaced" "$log" | sed 's/^ *"//;s/",$//' | head -10
  fi
  echo "--- import 以外の動き ---"
  grep -oE "# [a-zA-Z0-9_.\[\]\"-]+ will be (created|updated in-place|destroyed|replaced)" "$log" \
    | sort -u || echo "  (なし)"
  grep -iE "^ *\"?Error" "$log" | sed 's/^ *"//;s/",$//' | head -5 || true

  [ "$st" = SUCCEEDED ]
}

case "$ACTION" in
  plan)
    run_job plan
    ;;
  apply|import)
    # CLAUDE.md: apply は承認ゲート。ヘッドレス安全のため --apply 明示時のみ。
    # **plan は必ず先に回す。** apply はその plan だけを適用する（作り直さない）。
    run_job plan || exit 1
    if [ "$APPLY_FLAG" != "--apply" ]; then
      echo
      echo "== plan のみ実行（適用するには末尾に --apply）"
    else
      # **受け入れ条件は `0 to destroy`（ADR-0031）。** `--apply` は plan を読む前に
      # 指定するので、「何が起きるか知らないまま適用する」形になりうる。
      # 資源が消える計画は、明示の上書きが無い限り通さない。
      if [ "${LAST_DESTROY_COUNT:-0}" -gt 0 ] && [ "${ORM_ALLOW_DESTROY:-}" != "1" ]; then
        echo >&2
        echo "!! plan に destroy / replace が ${LAST_DESTROY_COUNT} 件ある。適用しない。" >&2
        echo "   意図した破棄なら ORM_ALLOW_DESTROY=1 を明示すること。" >&2
        exit 1
      fi
      echo
      run_job apply
      # **移行が済んだらローカル state を退避する。** 残しておくと同じ実資源を
      # ORM とローカルの2つの state が所有し、旧ディレクトリで `terraform destroy` を
      # 打った人が ORM 管理下の資源を消せてしまう。
      if [ "$ACTION" = import ]; then
        # **収束を確かめてから退避する。** 完了条件は「imports.tf 抜きで No changes」。
        # 確かめずに旧 state を片付けると、取り込み漏れや構成差分が残ったまま
        # 戻り道だけ畳むことになる。
        echo
        echo "== 収束確認: imports.tf 抜きで plan し直す"
        rm -f "$WORK/environments/dev/imports.tf"
        ( cd "$WORK" && rm -f "$ZIP" && zip -qr "$ZIP" environments modules \
            $( [ -f schema.yaml ] && echo schema.yaml ) )
        oci resource-manager stack update --stack-id "$STACK_ID" --region "$STACK_REGION" \
          --config-source "$ZIP" --working-directory environments/dev --force >/dev/null
        if ! run_job plan; then
          echo "!! 収束確認の plan が失敗した。ローカル state は退避しない。" >&2
          exit 1
        fi
        if ! grep -q "No changes" "${ORM_LOG_DIR:-/tmp}/orm-${ENV_NAME}-plan-${LAST_PLAN_JOB: -12}.log"; then
          echo "!! imports.tf 抜きで差分が残っている。移行は未完了。" >&2
          echo "   ローカル state は退避しない（戻せる状態を保つ）。" >&2
          exit 1
        fi
        if [ -f "$SRC/terraform.tfstate" ]; then
          retired="$SRC/terraform.tfstate.migrated-to-orm"
          mv "$SRC/terraform.tfstate" "$retired"
          echo
          echo "== 収束確認 OK。ローカル state を退避: ${retired}"
          echo "   ORM が正になった。ここから terraform を直接動かさないこと。"
          echo "   （復旧が要るときのために消さずに残してある）"
        fi
      fi
    fi
    ;;
  *) usage ;;
esac
