"""Select AI RAG(RAG-03)のADMINセットアップ(何度実行してもよい)。

1. 対象スキーマへ DBMS_CLOUD / DBMS_CLOUD_AI 系の EXECUTE 付与 + ネットワークACL
2. DBMS_CLOUD_ADMIN.ENABLE_RESOURCE_PRINCIPAL で OCI$RESOURCE_PRINCIPAL を使えるようにする

資格情報は ADB 自身の身分(リソースプリンシパル)を使う。開発者の ~/.oci/config から API キーを
抜き出して DB へ焼き込む JETUSE_OCI_CRED は廃止した(ADR-0021)。

実行: .venv/bin/python ops/setup-select-ai.py [--schema JETUSE_APP]
前提: 共有ウォレット(ADB_WALLET_DIR・既定 /tmp/jetusedev_wallet)、.env の ADB_ADMIN_PASSWORD
"""

import argparse

import _adb


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schema", default=_adb.env("ADB_USER", "JETUSE_APP"),
                    help="対象スキーマ(既定: .env の ADB_USER、無ければ JETUSE_APP)")
    args = ap.parse_args()
    schema = _adb.assert_schema(args.schema)

    print(f"== ADMIN: grants + ACL + resource principal ({schema}) ==")
    admin = _adb.connect("ADMIN", _adb.env("ADB_ADMIN_PASSWORD"))
    try:
        cur = admin.cursor()
        _adb.assert_target(admin)  # DDL の前に接続先 ADB を同定する（fail-closed）
        cur.execute("SELECT version_full FROM product_component_version")
        print("  db version:", cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM dba_users WHERE username = :u", u=schema)
        if cur.fetchone()[0] == 0:
            raise SystemExit(f"スキーマ {schema} が存在しない。先に ops/setup-dev-schema.py を実行する。")
        _adb.grant_cloud_packages(cur, schema)
        _adb.append_acl(cur, schema)
        _adb.enable_resource_principal(cur, schema)
        admin.commit()
    finally:
        admin.close()
    print("done")


if __name__ == "__main__":
    main()
