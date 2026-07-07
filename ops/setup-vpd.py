"""SP2-02 初回セットアップ(specs/18 §3.2.1・§4.3 — 人間承認のうえ実行)。

対象: (1) ADMIN からアプリスキーマへの最小権限付与
        - GRANT EXECUTE ON DBMS_RLS   (datasets 表への VPD ポリシー付与用)
        - GRANT CREATE ANY CONTEXT / DROP ANY CONTEXT (アプリケーションコンテキスト用)
      (1b) 排他リース最小カバーパッケージ(Gate 2 最小案・承認 2026-07-07):
        - ADMIN 所有 JETUSE_LOCK(ALLOCATE_UNIQUE/REQUEST/RELEASE のみ)を作成
        - アプリスキーマへ EXECUTE + private synonym のみ付与(DBMS_LOCK 直付けはしない)
      (2) アプリスキーマでの VPD 定義の適用(vpd.reapply_definitions —
          JETUSE_VPD_CTX / コンテキスト / JETUSE_VPD_POLICY)
      (3) 既存 JETUSE_DS_* 表への一括ポリシー付与(vpd.apply_policies_to_existing)
      (4) 完全性検証(vpd.verify_integrity — 問題ゼロで dbchat/datasets ゲートが開く)

排他リース(1b)は VPD の有無に依らず常に構成する。VPD 定義(2〜4)は VPD_ENABLED=true のときのみ。
実行前提: 人間承認の証跡が runs/<run-id>/e2e/APPROVAL.md にあること(CLAUDE.md 人間ゲート)。
実行(Internal): VPD_ENABLED=true ADB_ADMIN_PASSWORD=... .venv/bin/python ops/setup-vpd.py
実行(Public/既定): ADB_ADMIN_PASSWORD=... .venv/bin/python ops/setup-vpd.py
      → 排他リース最小カバーパッケージのみ構成し VPD はスキップ。
      (ADB_USER / ADB_PASSWORD / ADB_DSN / ADB_WALLET_* は .env / 環境変数)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "api"))

import oracledb  # noqa: E402

from jetuse_core import vpd  # noqa: E402
from jetuse_core.db import _wallet_dir  # noqa: E402
from jetuse_core.settings import get_settings  # noqa: E402

GRANTS = (
    "GRANT EXECUTE ON DBMS_RLS TO {app}",
    "GRANT CREATE ANY CONTEXT TO {app}",
    "GRANT DROP ANY CONTEXT TO {app}",
)


def main() -> None:
    s = get_settings()
    admin_pw = os.environ.get("ADB_ADMIN_PASSWORD", "")
    if not admin_pw:
        raise SystemExit("ADB_ADMIN_PASSWORD が未設定")
    wallet = _wallet_dir(s)
    admin = oracledb.connect(
        user="ADMIN", password=admin_pw, dsn=s.adb_dsn, config_dir=wallet,
        wallet_location=wallet, wallet_password=s.adb_wallet_password,
        tcp_connect_timeout=20.0,
    )
    cur = admin.cursor()
    # (1b) 排他リース最小カバーパッケージ(Gate 2 最小案)は VPD の有無に依らず必要 — Public/Internal
    # 双方の demo 操作で使う。ここで無条件に構成する(review-16 blocker: VPD 無効デプロイでも lease が
    # 使えるように)。旧 app 所有 package からの移行は排他崩壊を避けるため保守ウィンドウ必須:
    # LOCK_MIGRATION_APP_OFFLINE=true(= operator がアプリ停止/ドレインを明示)のときのみ移行する。
    app_offline = os.environ.get("LOCK_MIGRATION_APP_OFFLINE", "").lower() in ("1", "true", "yes")
    owner = vpd.provision_lock_for(cur, s.adb_user, app_offline=app_offline)
    admin.commit()
    print(f"OK: minimal lock cover package {owner}.JETUSE_LOCK + EXECUTE/synonym → {s.adb_user}")

    if not s.vpd_enabled:
        # Public/既定デプロイ: 排他リースだけ構成して終了。VPD 定義・ポリシーは無効なので触らない
        # (no-op を "verified" と誤表示しない — review-11 B002 の趣旨は維持)。
        print("VPD_ENABLED=false: 排他リースのみ構成(Public/既定)。VPD 定義・ポリシーはスキップ。")
        admin.close()
        return

    # --- VPD 有効時のみ: RLS/CONTEXT 付与 + VPD 定義 + 既存表への一括ポリシー + 完全性検証 ---
    for g in GRANTS:
        stmt = g.format(app=s.adb_user)
        cur.execute(stmt)
        print(f"OK: {stmt}")
    admin.commit()
    admin.close()

    vpd.reapply_definitions()
    print("OK: VPD definitions applied (JETUSE_VPD_CTX / context / policy fn)")
    applied = vpd.apply_policies_to_existing()
    print(f"OK: bulk ADD_POLICY applied to {len(applied)} existing tables: {applied}")
    problems = vpd.verify_integrity()
    if problems:
        print("NG: integrity problems remain (dbchat/datasets stay 503):")
        for p in problems:
            print(f"  - {p}")
        raise SystemExit(1)
    print("OK: VPD integrity verified — dbchat/datasets gate opens")


if __name__ == "__main__":
    main()
