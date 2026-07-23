"""Select AI(dbchat)をリソースプリンシパルで使うための ADMIN 一回セットアップ(承認済み手動手順)。

dev-app(jetuse-dev-app)は共有 loop ADB を指すため、自動 push デプロイでは ADB を変更しない
(RUN_DB_BOOTSTRAP を設定しない — codex review-1 B001)。dbchat を有効化する ADB 側の一回だけの
ADMIN 操作をここに切り出す(冪等・**明示の人間実行**):

  1. JETUSE_APP へ DBMS_CLOUD / DBMS_CLOUD_AI / _AI_AGENT / _PIPELINE の EXECUTE 付与
  2. GenAI / Object Storage への ネットワーク ACL(JETUSE_APP)
  3. DBMS_CLOUD_ADMIN.ENABLE_RESOURCE_PRINCIPAL(JETUSE_APP) — OCI$RESOURCE_PRINCIPAL を使えるように

前提: ADB の RP(動的グループ)に generative-ai-family + Object Storage read 権限(IAM)。
アプリ(dbchat)は OCI$RESOURCE_PRINCIPAL で Select AI を実行し、/api/health は uvicorn 内の
プローブ(nl2sql.select_ai_rp_status)で反映する(bootstrap 非依存)。

実行(承認の上): ADB_ADMIN_PASSWORD=... .venv/bin/python ops/setup-select-ai-rp.py
必要 env(.env): ADB_DSN / ADB_WALLET_PASSWORD / ADB_USER(既定 JETUSE_APP) / OCI_REGION、
             + ウォレット(ADB_OCID から generate-wallet 済み or 実行時 RP 生成)。
"""

import os
import pathlib

import oracledb

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV = dict(
    line.split("=", 1)
    for line in (ROOT / ".env").read_text().splitlines()
    if "=" in line and not line.startswith("#")
)
ADMIN_PW = os.environ.get("ADB_ADMIN_PASSWORD") or ENV.get("ADB_ADMIN_PASSWORD", "")
DSN = ENV["ADB_DSN"]
WALLET_PW = ENV["ADB_WALLET_PASSWORD"]
WALLET = os.environ.get("ADB_WALLET_DIR", "/tmp/jetusedev_wallet")
APP_USER = ENV.get("ADB_USER", "JETUSE_APP")
REGION = ENV.get("OCI_REGION", "ap-osaka-1")

ACL_HOSTS = [
    f"inference.generativeai.{REGION}.oci.oraclecloud.com",
    f"generativeai.{REGION}.oci.oraclecloud.com",
    f"objectstorage.{REGION}.oraclecloud.com",
]
PKGS = ("DBMS_CLOUD", "DBMS_CLOUD_AI", "DBMS_CLOUD_AI_AGENT", "DBMS_CLOUD_PIPELINE")


def main() -> None:
    if not ADMIN_PW:
        raise SystemExit("ADB_ADMIN_PASSWORD が未設定です(env で渡してください)。")
    conn = oracledb.connect(
        user="ADMIN", password=ADMIN_PW, dsn=DSN,
        config_dir=WALLET, wallet_location=WALLET, wallet_password=WALLET_PW,
        tcp_connect_timeout=25.0,
    )
    cur = conn.cursor()
    for pkg in PKGS:
        cur.execute(f"GRANT EXECUTE ON {pkg} TO {APP_USER}")
    print(f"grants: {', '.join(PKGS)} -> {APP_USER}")
    for host in ACL_HOSTS:
        cur.execute(
            """
            BEGIN
              DBMS_NETWORK_ACL_ADMIN.APPEND_HOST_ACE(
                host => :h,
                ace  => xs$ace_type(privilege_list => xs$name_list('http'),
                                    principal_name => :p, principal_type => xs_acl.ptype_db));
            END;""",
            h=host, p=APP_USER,
        )
        print(f"acl: {host}")
    cur.execute(
        "BEGIN DBMS_CLOUD_ADMIN.ENABLE_RESOURCE_PRINCIPAL(username => :u); END;", u=APP_USER
    )
    print(f"resource principal enabled for {APP_USER}")
    conn.commit()
    conn.close()
    print("done. dbchat(Select AI/RP)を有効化しました(/api/health は初回アクセスで反映)。")


if __name__ == "__main__":
    main()
