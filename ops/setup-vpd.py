"""SP2-02 初回セットアップ(specs/18 §3.2.1・§4.3 — 人間承認のうえ実行)。

対象: (1) ADMIN からアプリスキーマへの最小権限付与
        - GRANT EXECUTE ON DBMS_LOCK  (排他リース cover package 用)
        - GRANT EXECUTE ON DBMS_RLS   (datasets 表への VPD ポリシー付与用)
        - GRANT CREATE ANY CONTEXT / DROP ANY CONTEXT (アプリケーションコンテキスト用)
      (2) アプリスキーマでの承認済み定義の適用(vpd.approved_definitions —
          JETUSE_LOCK / JETUSE_VPD_CTX / コンテキスト / JETUSE_VPD_POLICY)
      (3) 既存 JETUSE_DS_* 表への一括ポリシー付与(vpd.apply_policies_to_existing)
      (4) 完全性検証(vpd.verify_integrity — 問題ゼロで dbchat/datasets ゲートが開く)

実行前提: 人間承認の証跡が runs/<run-id>/e2e/APPROVAL.md にあること(CLAUDE.md 人間ゲート)。
実行: VPD_ENABLED=true ADB_ADMIN_PASSWORD=... .venv/bin/python ops/setup-vpd.py
      (VPD_ENABLED=true 必須 — 無効だと VPD 定義適用が no-op になる。ADB_USER / ADB_PASSWORD /
       ADB_DSN / ADB_WALLET_* は .env / 環境変数)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "api"))

import oracledb  # noqa: E402

from jetuse_core import vpd  # noqa: E402
from jetuse_core.db import _wallet_dir  # noqa: E402
from jetuse_core.settings import get_settings  # noqa: E402

GRANTS = (
    "GRANT EXECUTE ON DBMS_LOCK TO {app}",
    "GRANT EXECUTE ON DBMS_RLS TO {app}",
    "GRANT CREATE ANY CONTEXT TO {app}",
    "GRANT DROP ANY CONTEXT TO {app}",
)


def main() -> None:
    s = get_settings()
    admin_pw = os.environ.get("ADB_ADMIN_PASSWORD", "")
    if not admin_pw:
        raise SystemExit("ADB_ADMIN_PASSWORD が未設定")
    # VPD セットアップは VPD 有効前提。無効のままだと reapply/apply/verify が全て no-op になり、
    # ポリシー未付与のまま "verified" と誤表示する(fail-open — codex review-11 B002)。
    if not s.vpd_enabled:
        raise SystemExit(
            "VPD_ENABLED=true が必要（VPD セットアップは VPD 有効前提）。実行例: "
            "VPD_ENABLED=true ADB_ADMIN_PASSWORD=... .venv/bin/python ops/setup-vpd.py")
    wallet = _wallet_dir(s)
    admin = oracledb.connect(
        user="ADMIN", password=admin_pw, dsn=s.adb_dsn, config_dir=wallet,
        wallet_location=wallet, wallet_password=s.adb_wallet_password,
        tcp_connect_timeout=20.0,
    )
    cur = admin.cursor()
    for g in GRANTS:
        stmt = g.format(app=s.adb_user)
        cur.execute(stmt)
        print(f"OK: {stmt}")
    admin.commit()
    admin.close()

    vpd.reapply_definitions()
    print("OK: approved definitions applied (JETUSE_LOCK / VPD context / policy fn)")
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
