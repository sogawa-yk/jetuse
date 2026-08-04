#!/usr/bin/env bash
# リージョンと OCIR ホストの解決(AGT-06)。ops/ の bash スクリプトから source する。
#
# かつて各スクリプトが `REGION=ap-osaka-1` と `kix.ocir.io` を直書きしていたため、
# シカゴへ配備しようとすると **リージョンだけ変えてもイメージは大阪の OCIR を指す**
# という食い違いが起きた。ここで一元的に解決し、直書きを残さない。
#
# 使い方:
#   . "$(dirname "$0")/_region.sh"
#   REGION=$(jetuse_region)              # env OCI_REGION > .env > 既定(us-chicago-1)
#   jetuse_use_cli_region "$REGION"      # **必ず呼ぶ**。以降の oci CLI がこのリージョンを向く
#   OCIR=$(jetuse_ocir_host "$REGION")   # 例 ord.ocir.io
#
# **`jetuse_use_cli_region` を忘れないこと。** `REGION` を計算しただけでは
# `oci` CLI は ~/.oci/config の既定プロファイル(大阪でありうる)を向いたままで、
# 「シカゴのつもりで大阪に作る」という最悪の取り違えが起きる(review-1 B003)。
# `--region` を各呼び出しに付ける方式は、後から足した呼び出しで抜ける。
# ここで一度 export するほうが漏れない。

# OCIR のホスト名はリージョンキー(3文字)。JetUse のイメージ push 先 4 リージョン
# (ADR-0011 / ADR-0017)を持つ。ここに無いリージョンは OCIR_HOST を明示指定する。
jetuse_ocir_host() {
  local region="$1"
  if [ -n "${OCIR_HOST:-}" ]; then echo "$OCIR_HOST"; return 0; fi
  case "$region" in
    us-chicago-1) echo "ord.ocir.io" ;;
    ap-osaka-1)   echo "kix.ocir.io" ;;
    ap-tokyo-1)   echo "nrt.ocir.io" ;;
    us-ashburn-1) echo "iad.ocir.io" ;;
    *)
      echo "未対応リージョン: $region (OCIR_HOST=<key>.ocir.io を明示指定してください)" >&2
      return 1
      ;;
  esac
}

# 以降の `oci` CLI 呼び出しを指定リージョンへ固定する。
# OCI CLI は OCI_CLI_REGION を --region より弱く、プロファイル既定より強く扱う。
jetuse_use_cli_region() {
  export OCI_CLI_REGION="$1"
}

# tfvars の `region = "..."` を取り出す。そのスタックの配備先の**正**はここ
# (terraform がこの値で配備する)。取れなければ空を返す。
# `sed -E ... \s` は BSD sed(macOS)で効かず、**壊れた値を黙って返す**ので awk を使う。
jetuse_region_from_tfvars() {
  [ -f "$1" ] || return 0
  awk -F'"' '/^[[:space:]]*region[[:space:]]*=/ { print $2; exit }' "$1"
}

# 配備先リージョン。env > .env > 既定。既定は AGT-06 でシカゴへ移した。
jetuse_region() {
  if [ -n "${OCI_REGION:-}" ]; then echo "$OCI_REGION"; return 0; fi
  local from_env=""
  [ -f .env ] && from_env=$(grep '^OCI_REGION=' .env | cut -d= -f2- || true)
  echo "${from_env:-us-chicago-1}"
}
