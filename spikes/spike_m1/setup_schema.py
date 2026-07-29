"""SPIKE-M1 の検証用スキーマ JETUSE_SPIKE_M1 を共有 loop ADB に作る（1 回だけ）。

ADB を増やさず、既存の共有 loop ADB にスキーマだけ足して隔離する（ユーザー指示）。
権限は ops/setup-dev-schema.py と同じ構成 + ベクタ系（DBMS_VECTOR_CHAIN）。

実行: .venv/bin/python spikes/spike_m1/setup_schema.py
片付け: .venv/bin/python spikes/spike_m1/teardown.py
"""

import json
import os
import re
import sys

import oracledb

from common import (
    CRED,
    SCHEMA,
    VEC_CRED,
    banner,
    connect,
    connect_admin,
    is_ours,
    load_env,
    oci_api_key,
    record_created,
    schema_key,
)

ACL_HOSTS = [
    "inference.generativeai.{region}.oci.oraclecloud.com",
    "generativeai.{region}.oci.oraclecloud.com",
    "objectstorage.{region}.oraclecloud.com",
]


def _redact(sql: str) -> str:
    """IDENTIFIED BY の実値をログに出さない（証跡をそのまま貼れるようにする）。"""
    return re.sub(r'IDENTIFIED BY ".*"', 'IDENTIFIED BY "***"', sql)


def _try(cur: oracledb.Cursor, sql: str, *, ignore: tuple[str, ...] = ()) -> None:
    shown = _redact(sql)
    try:
        cur.execute(sql)
        print(f"  ok: {shown[:78]}")
    except oracledb.DatabaseError as e:
        msg = str(e)
        if any(code in msg for code in ignore):
            print(f"  skip: {shown[:60]} ({msg.splitlines()[0]})")
            return
        raise


def main() -> None:
    env = load_env()
    pw = env["ADB_PASSWORD"]
    region = env["OCI_REGION"]

    banner(f"ADMIN: create {SCHEMA}")
    admin = connect_admin()   # 接続先ガード（共有 loop ADB か）は connect_admin 内で通る
    cur = admin.cursor()
    cur.execute("SELECT banner_full FROM v$version")
    print("db:", cur.fetchone()[0])

    # 既存ユーザーへ黙って ALTER/GRANT/ACL を打たない。
    # 自分が作った（台帳にある）ものだけ再利用し、それ以外は同名でも中止する。
    cur.execute("SELECT COUNT(*) FROM all_users WHERE username = :u", u=SCHEMA)
    exists = cur.fetchone()[0] > 0
    # 台帳のキーは作成時刻を含むので**ユーザーが在る状態で**計算する
    if exists and not is_ours("db_schema", schema_key(admin)):
        sys.exit(f"ユーザー {SCHEMA} は既に存在するが台帳に無い。"
                 " 別用途のスキーマを書き換える恐れがあるため中止する。"
                 " 自分のものだと分かっている場合だけ台帳に足すか、先に teardown すること。")
    if not exists:
        _try(cur, f'CREATE USER {SCHEMA} IDENTIFIED BY "{pw}"')
        record_created("db_schema", schema_key(admin), SCHEMA)
    else:
        print(f"  既存の {SCHEMA} を再利用（台帳に記録済み）")
        # 再実行時にパスワードを .env と一致させる（同一値なら ORA-28007 で無害に落ちる）
        _try(cur, f'ALTER USER {SCHEMA} IDENTIFIED BY "{pw}"', ignore=("ORA-28007",))
    _try(cur, f"GRANT CREATE SESSION, RESOURCE, CREATE VIEW TO {SCHEMA}")
    _try(cur, f"ALTER USER {SCHEMA} QUOTA UNLIMITED ON DATA")
    for pkg in ("DBMS_CLOUD", "DBMS_CLOUD_AI", "DBMS_CLOUD_PIPELINE", "DBMS_VECTOR_CHAIN"):
        _try(cur, f"GRANT EXECUTE ON {pkg} TO {SCHEMA}", ignore=("ORA-04043",))
    for host in ACL_HOSTS:
        cur.execute(
            """
            BEGIN
              DBMS_NETWORK_ACL_ADMIN.APPEND_HOST_ACE(
                host => :h,
                ace  => xs$ace_type(privilege_list => xs$name_list('http'),
                                    principal_name => :p,
                                    principal_type => xs_acl.ptype_db));
            END;""",
            h=host.format(region=region), p=SCHEMA,
        )
        print(f"  ACL: {host.format(region=region)}")
    admin.commit()
    admin.close()

    banner(f"{SCHEMA}: credential {CRED} / {VEC_CRED}")
    conn = connect(SCHEMA, pw)
    cur = conn.cursor()
    conf = oci_api_key()
    # fingerprint は認証情報なのでログに出さない（証跡がそのままコミットされる）
    print(f"  使用プロファイル: [{os.environ.get('OCI_PROFILE') or 'DEFAULT'}]")
    # 作り直し（旧い誤った資格証明が残っていると全呼び出しが ORA-20404 になる）
    for drop in (f"BEGIN DBMS_CLOUD.DROP_CREDENTIAL('{CRED}'); END;",
                 f"BEGIN DBMS_VECTOR_CHAIN.DROP_CREDENTIAL('{VEC_CRED}'); END;"):
        try:
            cur.execute(drop)
        except oracledb.DatabaseError as e:
            print(f"  drop skip: {str(e).splitlines()[0]}")
    # ① DBMS_CLOUD 用（Select AI / Object Storage アクセス）
    cur.execute(
        """
        BEGIN
          DBMS_CLOUD.CREATE_CREDENTIAL(
            credential_name => :c, user_ocid => :u, tenancy_ocid => :t,
            private_key => :k, fingerprint => :f);
        END;""",
        c=CRED, u=conf["user"], t=conf["tenancy"],
        k=conf["private_key"], f=conf["fingerprint"],
    )
    print(f"  {CRED} created (DBMS_CLOUD)")
    # ② DBMS_VECTOR_CHAIN 用（DB 内埋め込み。資格証明ストアが別）
    cur.execute(
        "BEGIN DBMS_VECTOR_CHAIN.CREATE_CREDENTIAL("
        "credential_name => :c, params => JSON(:p)); END;",
        c=VEC_CRED,
        p=json.dumps({
            "user_ocid": conf["user"], "tenancy_ocid": conf["tenancy"],
            "compartment_ocid": env["COMPARTMENT_OCID"],
            "private_key": conf["private_key"], "fingerprint": conf["fingerprint"],
        }),
    )
    print(f"  {VEC_CRED} created (DBMS_VECTOR_CHAIN)")
    conn.commit()
    conn.close()
    print("\ndone")


if __name__ == "__main__":
    main()
