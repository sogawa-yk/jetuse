#!/usr/bin/env bash
# 開発者ごとのE2E環境を作成/更新する(共有基盤は environments/dev のまま流用)。
# APIイメージを本人タグでbuild/push → app스タックをterraform apply → SPAをbuild/配信 → URL表示。
# 前提:
#   - 共有 environments/dev を一度 `terraform apply` 済み(新出力が state に反映されている)
#   - 本人スキーマを ops/setup-dev-schema.py --dev <dev> で作成済み
#   - infra/terraform/environments/app/<dev>.tfvars を用意済み(alice.tfvars.example 参照)
#   - OCIRログイン済み / .env に OCIR_TOKEN 等
# 使い方: ops/dev-env-up.sh <dev>
set -euo pipefail
cd "$(dirname "$0")/.."

APPLY=0; DEV=""
for a in "$@"; do case "$a" in --apply) APPLY=1;; -*) echo "unknown flag: $a" >&2; exit 2;; *) DEV="$a";; esac; done
[ -n "$DEV" ] || { echo "usage: dev-env-up.sh <dev> [--apply]"; exit 1; }
APPDIR=infra/terraform/environments/app
TFVARS="$APPDIR/${DEV}.tfvars"
[ -f "$TFVARS" ] || { echo "missing $TFVARS (copy alice.tfvars.example)"; exit 1; }

# OCIR の名前空間はテナンシ固有なので**リポジトリに埋めない**（規約）。
# Object Storage の名前空間と同じ値なので、.env の OS_NAMESPACE を既定に使う。
# **.env は source していない**（shell 変数として export されない）ので、
# 環境変数ではなくファイルから読む。優先順: 環境変数 > .env の OCIR_NAMESPACE > .env の OS_NAMESPACE。
# **空振りを成功扱いにする（|| true）。** set -euo pipefail 下では grep の「一致なし」(rc=1)が
# pipefail でパイプライン全体の失敗になり、`NS="${OCIR_NAMESPACE:-$(_ns_from_env_file ...)}"` の
# 代入で set -e が発火してスクリプトごと終了する。つまり下の OS_NAMESPACE フォールバックには
# **到達できなかった**（.env に OCIR_NAMESPACE を書いていない環境で配備が無言で rc=1 になる実害）。
_ns_from_env_file() { grep "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"'\r' || true; }
NS="${OCIR_NAMESPACE:-$(_ns_from_env_file OCIR_NAMESPACE)}"
[ -n "$NS" ] || NS="$(_ns_from_env_file OS_NAMESPACE)"
# どちらも無いなら止める（誤ったテナンシの repository を掴まないため）
[ -n "$NS" ] || { echo "OCIR_NAMESPACE か OS_NAMESPACE を .env に設定してください" >&2; exit 1; }
SHA=$(git rev-parse --short HEAD)
TAG="dev-${DEV}-${SHA}"
# イメージの置き場は**このスタックの配備先リージョン**に合わせる(AGT-06)。
# 正は <dev>.tfvars の region(terraform がそれで配備するため)。無ければ .env / 既定。
# kix.ocir.io を直書きしていたときは、region=us-chicago-1 にしても
# **イメージだけ大阪へ push され**、シカゴのコンテナが pull できなかった。
. "$(dirname "$0")/_region.sh"
REGION=$(jetuse_region_from_tfvars "$TFVARS")
REGION="${REGION:-$(jetuse_region)}"
jetuse_use_cli_region "$REGION"
IMAGE="$(jetuse_ocir_host "$REGION")/${NS}/jetuse-dev-api:${TAG}"
echo "== region=${REGION} (tfvars 由来) / image registry=$(jetuse_ocir_host "$REGION")"

. "$(dirname "$0")/_container.sh"
CE=$(jetuse_container_engine) || exit 1
echo "== build & push ${IMAGE} (engine=${CE} platform=${JETUSE_BUILD_PLATFORM:-linux/amd64})"
# ビルドコンテキストはリポジトリルート(Containerfile が packages/jetuse_shared を取り込むため。P1b)
# **--platform を固定する。** Apple Silicon で素直に build すると arm64 イメージになり、
# Container Instance の shape(x86)が受け付けず apply が
# 「A container image provided is not compatible with the processor architecture」で落ちる。
# しかも image_url の変更は**置換**なので旧インスタンスは先に削除済み＝
# **環境が落ちたまま復旧できない**（2026-08-04 に実際に踏んだ）。
# CI(ubuntu/x86)では素の build でも通るため、ローカル(Apple Silicon)だけが壊れていた。
BUILD_PLATFORM="${JETUSE_BUILD_PLATFORM:-linux/amd64}"
"$CE" build --platform "$BUILD_PLATFORM" -f packages/api/Containerfile -t "${IMAGE}" .
"$CE" push "${IMAGE}"

# 共有基盤の state は **ORM が持つ**（ADR-0031）。`terraform_remote_state` は ORM を直接
# 読めないので、先に落としてローカルファイルとして渡す。**ローカルの
# environments/dev/terraform.tfstate は移行後は更新されない**ので、そのまま読むと古い値を掴む。
SHARED_ENV="${JETUSE_SHARED_ENV:-internal-dev}"
SHARED_STATE="${JETUSE_SHARED_STATE:-}"
if [ -z "$SHARED_STATE" ]; then
  # **mktemp が返したパスをそのまま使う。** `$(mktemp ...).tfstate` と後置すると、
  # 実際の書き込み先は mktemp 管理外の別ファイルになり、権限が umask 任せになる。
  # 共有 state には ADB のパスワード等が入りうるので 0600 を保ち、終了時に消す。
  SHARED_STATE="$(mktemp -t jetuse-shared-XXXXXX)"
  chmod 600 "$SHARED_STATE"
  trap 'rm -f "$SHARED_STATE"' EXIT
  echo "== 共有基盤の state を ORM から取得 (${SHARED_ENV})"
  ops/orm-stack.sh "$SHARED_ENV" state > "$SHARED_STATE"
  # 落とせたことを確かめる。空や壊れた state を渡すと、terraform は
  # 「共有基盤の出力が無い」と言うだけで**原因が ORM 取得の失敗だと分からない**。
  python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))
n=len(d.get('outputs') or {})
if n == 0:
    sys.exit('共有 state に outputs が無い（ORM からの取得に失敗している可能性）')
print(f'   outputs {n} 個')
" "$SHARED_STATE"
fi

echo "== terraform plan (state: ${DEV}.tfstate)"
( cd "$APPDIR"
  terraform init -input=false >/dev/null
  terraform plan -input=false -var-file="${DEV}.tfvars" \
    -var "api_image_url=${IMAGE}" -var "shared_state_path=${SHARED_STATE}" \
    -state="${DEV}.tfstate" -out="${DEV}.tfplan"
)
# CLAUDE.md: terraform apply は承認ゲート。ヘッドレス安全のため対話確認はせず、--apply 明示時のみ適用する。
if [ "$APPLY" -ne 1 ]; then
  echo "== plan のみ完了（適用するには --apply を渡す）。SPA 配信もスキップ。"
  exit 0
fi
( cd "$APPDIR" && terraform apply -input=false -state="${DEV}.tfstate" "${DEV}.tfplan" )

echo "== build & deploy SPA -> jetuse-${DEV}-spa"
( cd packages/web && npm run build && bash scripts/deploy.sh "jetuse-${DEV}-spa" )

HOST=$(cd "$APPDIR" && terraform output -state="${DEV}.tfstate" -raw apigw_hostname)
echo ""
echo "== done: https://${HOST}/"
echo "   API: https://${HOST}/api/chat/models"
