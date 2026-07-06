#!/usr/bin/env sh
# APIコンテナ起動エントリポイント(INFRA-03 ORMワンクリック)。
# RUN_DB_BOOTSTRAP=true のとき、ADBスキーマ用意+マイグレーションを先に実行してから uvicorn 起動。
# ブートストラップが失敗してもAPIは起動する(DB系は503でフェイルセーフ)。
set -e

# 起動世代トークン。bootstrap(reconcile→upload gate 開)と uvicorn(upload gate 参照)は別
# プロセスだが同一コンテナ起動なので、ここで一度だけ生成して両者へ export する。前回起動が
# 残した gate 'Y' は boot_id 不一致で無効化され、今回の reconcile 完了まで upload は fail-closed
# に保たれる(SP2-02 / codex review-8 B001)。未設定でも動くが並行起動の窓が塞がらない。
if [ -z "${APP_BOOT_ID}" ]; then
  APP_BOOT_ID="$(cat /proc/sys/kernel/random/uuid 2>/dev/null || python -c 'import uuid;print(uuid.uuid4())')"
fi
export APP_BOOT_ID
echo "[entrypoint] APP_BOOT_ID=${APP_BOOT_ID}"

# DBブートストラップ(スキーマ作成+マイグレ)は**バックグラウンド**で実行し、APIは即起動する。
# これにより ADB ACTIVE 待ちやプロビジョニング中も API は応答(DB系は503でフェイルセーフ)し、
# ゲートウェイが長時間502になるのを避ける。完了後にDB系が利用可能になる。
if [ "${RUN_DB_BOOTSTRAP}" = "true" ]; then
  echo "[entrypoint] starting DB bootstrap in background (schema + migrate)..."
  python -m jetuse_core.bootstrap &
fi

exec uvicorn service.main:app --host 0.0.0.0 --port 8000
