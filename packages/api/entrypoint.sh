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

# SP3-07: ORASEJAPAN 共有テナンシ(生成 gpt-5 系)のユーザープリンシパル材料を
# RM sensitive 変数 → コンテナ env で受け取り、~/.oci/config のプロファイルへ冪等に書き出す。
# 材料が不完全/不正 base64 のときは config を書かず GEN_SHARED_PROFILE を落として起動を続ける
# (= 共有モデルだけ sign_proxy が 403 の fail-closed。任意機能の不備で API 全体を殺さない —
# review-1 M002)。値はログに出さない。
if [ -n "${GEN_SHARED_PROFILE}" ]; then
  if [ -n "${GEN_SHARED_KEY_PEM_B64}" ] && [ -n "${GEN_SHARED_USER_OCID}" ] \
    && [ -n "${GEN_SHARED_TENANCY_OCID}" ] && [ -n "${GEN_SHARED_FINGERPRINT}" ] \
    && KEY_PEM="$(echo "${GEN_SHARED_KEY_PEM_B64}" | base64 -d 2>/dev/null)" \
    && [ -n "${KEY_PEM}" ]; then
    OCI_DIR="${HOME:-/root}/.oci"
    # umask 077 = ディレクトリ 700 / ファイル 600 で生成(鍵材料を他ユーザーから読ませない)
    (
      umask 077
      mkdir -p "${OCI_DIR}"
      printf '%s\n' "${KEY_PEM}" > "${OCI_DIR}/gen_shared_key.pem"
      cat > "${OCI_DIR}/config" <<EOF
[${GEN_SHARED_PROFILE}]
user=${GEN_SHARED_USER_OCID}
fingerprint=${GEN_SHARED_FINGERPRINT}
tenancy=${GEN_SHARED_TENANCY_OCID}
region=${GEN_SHARED_REGION:-ap-osaka-1}
key_file=${OCI_DIR}/gen_shared_key.pem
EOF
    )
    unset KEY_PEM
    echo "[entrypoint] wrote OCI profile ${GEN_SHARED_PROFILE} for shared-tenancy generation"
  else
    echo "[entrypoint] WARN: GEN_SHARED_* incomplete or invalid base64 — shared-tenancy generation disabled (fail-closed)"
    unset GEN_SHARED_PROFILE
  fi
fi
# 鍵材料はファイル化(または破棄)済み — 長寿命の API プロセスとその子へは伝播させない(review-2 m002)
unset GEN_SHARED_KEY_PEM_B64

# DBブートストラップ(スキーマ作成+マイグレ)は**バックグラウンド**で実行し、APIは即起動する。
# これにより ADB ACTIVE 待ちやプロビジョニング中も API は応答(DB系は503でフェイルセーフ)し、
# ゲートウェイが長時間502になるのを避ける。完了後にDB系が利用可能になる。
if [ "${RUN_DB_BOOTSTRAP}" = "true" ]; then
  echo "[entrypoint] starting DB bootstrap in background (schema + migrate)..."
  python -m jetuse_core.bootstrap &
fi

exec uvicorn service.main:app --host 0.0.0.0 --port 8000
