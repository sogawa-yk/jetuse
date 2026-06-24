"""Select AI RAG(RAG-03)のADMINセットアップ(1回実行)。

1. JETUSE_APPへ DBMS_CLOUD / DBMS_CLOUD_AI のEXECUTE付与 + ネットワークACL
2. JETUSE_APPスキーマにAPIキーcredential JETUSE_OCI_CRED を作成
   (Vault/リソースプリンシパル化はPhase 8)

実行: .venv/bin/python ops/setup-select-ai.py
前提: jetusedevウォレット /tmp/jetusedev_wallet、~/.oci/config のAPIキー
"""

import pathlib

import oracledb

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV = dict(
    line.split("=", 1)
    for line in (ROOT / ".env").read_text().splitlines()
    if "=" in line and not line.startswith("#")
)
TFVARS = (ROOT / "infra/terraform/environments/dev/terraform.tfvars").read_text()
WALLET_PW = next(
    line.split('"')[1] for line in TFVARS.splitlines() if "ADB_WALLET_PASSWORD" in line
)
APP_PW = ENV["ADB_APP_PASSWORD"]
WALLET = "/tmp/jetusedev_wallet"
REGION = ENV["OCI_REGION"]

ACL_HOSTS = [
    f"inference.generativeai.{REGION}.oci.oraclecloud.com",
    f"generativeai.{REGION}.oci.oraclecloud.com",
    f"objectstorage.{REGION}.oraclecloud.com",
]


def conn_as(user: str, pw: str) -> oracledb.Connection:
    return oracledb.connect(
        user=user, password=pw, dsn="jetusedev_low",
        config_dir=WALLET, wallet_location=WALLET, wallet_password=WALLET_PW,
        tcp_connect_timeout=20.0,
    )


def main():
    print("== ADMIN: grants + ACL ==")
    admin = conn_as("ADMIN", ENV["ADB_ADMIN_PASSWORD"])
    cur = admin.cursor()
    cur.execute("SELECT version_full FROM product_component_version")
    print("db version:", cur.fetchone()[0])
    cur.execute("GRANT EXECUTE ON DBMS_CLOUD TO JETUSE_APP")
    cur.execute("GRANT EXECUTE ON DBMS_CLOUD_AI TO JETUSE_APP")
    # Select AI Agent フレームワーク(ENH-04/SPIKE-E1)。これが無いとPLS-00201で使えない
    cur.execute("GRANT EXECUTE ON DBMS_CLOUD_AI_AGENT TO JETUSE_APP")
    # ベクトル索引はDBMS_CLOUD_PIPELINEで同期される(実機: ORA-20000で発覚)
    cur.execute("GRANT EXECUTE ON DBMS_CLOUD_PIPELINE TO JETUSE_APP")
    for host in ACL_HOSTS:
        cur.execute("""
            BEGIN
              DBMS_NETWORK_ACL_ADMIN.APPEND_HOST_ACE(
                host => :h,
                ace  => xs$ace_type(privilege_list => xs$name_list('http'),
                                    principal_name => 'JETUSE_APP',
                                    principal_type => xs_acl.ptype_db));
            END;""", h=host)
        print(f"  ACL: {host}")
    admin.commit()
    admin.close()

    print("== JETUSE_APP: credential ==")
    app = conn_as("JETUSE_APP", APP_PW)
    cur = app.cursor()
    oci_conf = dict(
        line.replace(" ", "").split("=", 1)
        for line in pathlib.Path("~/.oci/config").expanduser().read_text().splitlines()
        if "=" in line
    )
    key = pathlib.Path(oci_conf["key_file"]).expanduser().read_text()
    try:
        cur.execute("""
            BEGIN
              DBMS_CLOUD.CREATE_CREDENTIAL(
                credential_name => 'JETUSE_OCI_CRED',
                user_ocid       => :u,
                tenancy_ocid    => :t,
                private_key     => :k,
                fingerprint     => :f);
            END;""",
            u=oci_conf["user"], t=oci_conf["tenancy"],
            # OCI_API_KEYマーカー行の除去必須(SPIKE-04)
            k="".join(
                line for line in key.splitlines()
                if line and "-----" not in line and line != "OCI_API_KEY"
            ),
            f=oci_conf["fingerprint"])
        print("  credential created")
    except oracledb.DatabaseError as e:
        if "already exists" in str(e):
            print("  credential already exists")
        else:
            raise
    app.commit()
    app.close()
    print("done")


if __name__ == "__main__":
    main()
