"""開発者ごとのADBスキーマを共有ADBに作成する(1回/開発者)。

共有ADB(jetusedev_low)に本人用のアプリスキーマ JETUSE_<DEV> と読取専用 JETUSE_<DEV>_Q を作り、
データセット機能(Select AI)に必要な権限・ネットワークACL・JETUSE_OCI_CRED を付与し、
最後にマイグレーションを本人スキーマへ適用する。

採用方針 docs/guides/dev-environments.md / 設計 environments/app を参照。

実行: .venv/bin/python ops/setup-dev-schema.py --dev alice
前提: ~/.oci/config(APIキー)、共有ウォレット /tmp/jetusedev_wallet、.env と
      infra/terraform/environments/dev/terraform.tfvars(ADMIN/ウォレットPW参照)
"""

import argparse
import os
import pathlib
import re
import secrets
import string
import subprocess
import sys

import oracledb

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV = dict(
    line.split("=", 1)
    for line in (ROOT / ".env").read_text().splitlines()
    if "=" in line and not line.startswith("#")
)
TFVARS = (ROOT / "infra/terraform/environments/dev/terraform.tfvars").read_text()


def _tfvar(name: str) -> str:
    m = re.search(rf'{name}\s*=\s*"([^"]+)"', TFVARS)
    if not m:
        raise SystemExit(f"{name} not found in dev/terraform.tfvars")
    return m.group(1)


WALLET = "/tmp/jetusedev_wallet"
WALLET_PW = _tfvar("ADB_WALLET_PASSWORD")
DSN = "jetusedev_low"
REGION = ENV["OCI_REGION"]
ACL_HOSTS = [
    f"inference.generativeai.{REGION}.oci.oraclecloud.com",
    f"generativeai.{REGION}.oci.oraclecloud.com",
    f"objectstorage.{REGION}.oraclecloud.com",
]


def _conn(user: str, pw: str) -> oracledb.Connection:
    return oracledb.connect(
        user=user, password=pw, dsn=DSN,
        config_dir=WALLET, wallet_location=WALLET, wallet_password=WALLET_PW,
        tcp_connect_timeout=20.0,
    )


def _gen_pw() -> str:
    # Oracleパスワード要件を満たす(英大小+数字+記号、先頭は英字)
    pool = string.ascii_letters + string.digits
    body = "".join(secrets.choice(pool) for _ in range(16))
    return "Gx" + body + "#7"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", required=True, help="開発者識別子(英小文字)。例: alice")
    ap.add_argument("--app-password", default="", help="JETUSE_<DEV>のパスワード(未指定で自動生成)")
    ap.add_argument("--query-password", default="", help="JETUSE_<DEV>_Qのパスワード(未指定で自動生成)")
    ap.add_argument("--skip-migrate", action="store_true", help="マイグレーション実行を省略")
    args = ap.parse_args()

    if not re.fullmatch(r"[a-z][a-z0-9]{1,12}", args.dev):
        raise SystemExit("--dev は英小文字+数字、先頭英字、2〜13文字")

    app_user = f"JETUSE_{args.dev.upper()}"
    qry_user = f"{app_user}_Q"
    app_pw = args.app_password or _gen_pw()
    qry_pw = args.query_password or _gen_pw()

    print(f"== ADMIN: create {app_user} / {qry_user} ==")
    admin = _conn("ADMIN", ENV["ADB_ADMIN_PASSWORD"])
    cur = admin.cursor()
    # アプリスキーマ(CREATE TABLE/VIEW + データセットのSelect AI実行に必要なDBMS_CLOUD系)
    cur.execute(f'CREATE USER {app_user} IDENTIFIED BY "{app_pw}"')
    cur.execute(f"GRANT CREATE SESSION, RESOURCE, CREATE VIEW TO {app_user}")
    cur.execute(f"ALTER USER {app_user} QUOTA UNLIMITED ON DATA")
    for pkg in ("DBMS_CLOUD", "DBMS_CLOUD_AI", "DBMS_CLOUD_AI_AGENT", "DBMS_CLOUD_PIPELINE"):
        cur.execute(f"GRANT EXECUTE ON {pkg} TO {app_user}")
    for host in ACL_HOSTS:
        cur.execute("""
            BEGIN
              DBMS_NETWORK_ACL_ADMIN.APPEND_HOST_ACE(
                host => :h,
                ace  => xs$ace_type(privilege_list => xs$name_list('http'),
                                    principal_name => :p,
                                    principal_type => xs_acl.ptype_db));
            END;""", h=host, p=app_user)
    # 読取専用ユーザー(CREATE SESSIONのみ。datasetsが個別表にSELECTを付与する)
    cur.execute(f'CREATE USER {qry_user} IDENTIFIED BY "{qry_pw}"')
    cur.execute(f"GRANT CREATE SESSION TO {qry_user}")
    admin.commit()
    admin.close()
    print(f"  created {app_user}, {qry_user} (+grants, ACL)")

    print(f"== {app_user}: credential JETUSE_OCI_CRED ==")
    app = _conn(app_user, app_pw)
    cur = app.cursor()
    oci_conf = dict(
        line.replace(" ", "").split("=", 1)
        for line in pathlib.Path("~/.oci/config").expanduser().read_text().splitlines()
        if "=" in line
    )
    key = pathlib.Path(oci_conf["key_file"]).expanduser().read_text()
    cur.execute("""
        BEGIN
          DBMS_CLOUD.CREATE_CREDENTIAL(
            credential_name => 'JETUSE_OCI_CRED',
            user_ocid       => :u, tenancy_ocid => :t,
            private_key     => :k, fingerprint  => :f);
        END;""",
        u=oci_conf["user"], t=oci_conf["tenancy"],
        k="".join(
            line for line in key.splitlines()
            if line and "-----" not in line and line != "OCI_API_KEY"
        ),
        f=oci_conf["fingerprint"])
    app.commit()
    app.close()
    print("  credential created")

    if not args.skip_migrate:
        print(f"== migrate -> schema {app_user} ==")
        env = {
            **os.environ,
            "ADB_USER": app_user, "ADB_PASSWORD": app_pw,
            "ADB_DSN": DSN, "ADB_WALLET_DIR": WALLET, "ADB_WALLET_PASSWORD": WALLET_PW,
        }
        r = subprocess.run(
            [sys.executable, "-m", "jetuse_core.migrate"],
            cwd=str(ROOT / "packages/api"), env=env,
        )
        if r.returncode != 0:
            raise SystemExit("migration failed")

    print("\n== done ==")
    print("次の手順: infra/terraform/environments/app/<dev>.tfvars に以下を設定")
    print(f"  adb_user       = \"{app_user}\"")
    print(f"  adb_query_user = \"{qry_user}\"")
    print("  api_environment = { ... , ADB_PASSWORD = <下記>, ADB_QUERY_PASSWORD = <下記> }")
    print(f"  ADB_PASSWORD       (JETUSE app)  = {app_pw}")
    print(f"  ADB_QUERY_PASSWORD (JETUSE read) = {qry_pw}")
    print("その後: ops/dev-env-up.sh", args.dev)


if __name__ == "__main__":
    main()
