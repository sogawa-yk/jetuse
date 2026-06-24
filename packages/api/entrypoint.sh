#!/usr/bin/env sh
# APIコンテナ起動エントリポイント(INFRA-03 ORMワンクリック)。
# RUN_DB_BOOTSTRAP=true のとき、ADBスキーマ用意+マイグレーションを先に実行してから uvicorn 起動。
# ブートストラップが失敗してもAPIは起動する(DB系は503でフェイルセーフ)。
set -e

# DBブートストラップ(スキーマ作成+マイグレ)は**バックグラウンド**で実行し、APIは即起動する。
# これにより ADB ACTIVE 待ちやプロビジョニング中も API は応答(DB系は503でフェイルセーフ)し、
# ゲートウェイが長時間502になるのを避ける。完了後にDB系が利用可能になる。
if [ "${RUN_DB_BOOTSTRAP}" = "true" ]; then
  echo "[entrypoint] starting DB bootstrap in background (schema + migrate)..."
  python -m jetuse_core.bootstrap &
fi

exec uvicorn service.main:app --host 0.0.0.0 --port 8000
