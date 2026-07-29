#!/bin/sh
# PORT-03: ホスト型エージェントを削除する（destroy-time provisioner から呼ばれる）。
#
# ACTIVE な Hosted Deployment は直接削除できず、Application を削除すると
# デプロイメントがカスケード削除される（ops/recreate-agents.sh の実機記録）。
#
# 入力（環境変数）: HA_REGION HA_COMPARTMENT HA_NAME HA_OWNER_TAG
#
# 方針:
# - 所有者タグが一致するものだけを削除する。同名の管理外リソースには触れない。
# - **API 失敗を「もう無い」と取り違えない**。取り違えると、実体が残ったまま
#   state からだけ消えて Terraform から回収できない孤児になる。
#   OCI は「不存在」も「権限不足」も 404 + NotAuthorizedOrNotFound で返すため、
#   404 単独では判断せず一覧で確かめる（lib.sh の app_gone）。
set -eu

. "$(dirname "$0")/lib.sh"

find_owned_app
APP="$OWNED_APP"

if [ -z "$APP" ]; then
  # 一覧・詳細の取得に成功したうえで、自分のものが無い＝削除するものは無い。
  if [ "$FOREIGN_APP_EXISTS" = 1 ]; then
    echo "同名の Hosted Application $HA_NAME はこのスタックが作ったものではないため削除しません"
  else
    echo "Hosted Application $HA_NAME は既に存在しません"
  fi
  exit 0
fi

delete_app_and_wait "$APP"
echo "deleted hosted agent: $HA_NAME"
