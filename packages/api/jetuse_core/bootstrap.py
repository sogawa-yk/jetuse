"""DB自己ブートストラップ(INFRA-03 ORMワンクリックデプロイ)。

コンテナ起動時に ADMIN で接続し、アプリスキーマ(JETUSE_APP / JETUSE_QUERY)・権限・
ネットワークACL を**冪等に**用意してからマイグレーションを適用する。
`ops/setup-dev-schema.py` のDDL/ACLを移植(人手前提を排除)。

- ADBは作成直後ACTIVEになるまで時間がかかるため、接続成功まで上限付きで再試行する。
- Select AI(データセット)用クレデンシャルはリソースプリンシパルを有効化(best-effort)。
  APIキー前提の JETUSE_OCI_CRED は RP 環境では作れないため、ENABLE_RESOURCE_PRINCIPAL で
  OCI$RESOURCE_PRINCIPAL を使えるようにする(settings.select_ai_credential で参照)。
- 失敗してもAPI自体は起動する(DB系エンドポイントは503でフェイルセーフ)。

エントリポイント(entrypoint.sh)から `RUN_DB_BOOTSTRAP=true` のとき呼ばれる。
"""

import logging
import os
import time

import oracledb

from .db import _wallet_dir
from .settings import Settings, get_settings

logger = logging.getLogger("jetuse.bootstrap")

# ADB ACTIVE 待ちの上限(秒)と間隔。ADB作成は実測10-15分。
BOOTSTRAP_TIMEOUT_S = int(os.environ.get("DB_BOOTSTRAP_TIMEOUT_S", "1500"))
RETRY_INTERVAL_S = int(os.environ.get("DB_BOOTSTRAP_INTERVAL_S", "20"))

_DBMS_CLOUD_PKGS = ("DBMS_CLOUD", "DBMS_CLOUD_AI", "DBMS_CLOUD_AI_AGENT", "DBMS_CLOUD_PIPELINE")


def _ora_code(e: oracledb.DatabaseError) -> int:
    try:
        return e.args[0].code
    except Exception:  # noqa: BLE001
        return -1


def _admin_conn(settings: Settings, wallet_dir: str, admin_password: str) -> oracledb.Connection:
    return oracledb.connect(
        user="ADMIN",
        password=admin_password,
        dsn=settings.adb_dsn,
        config_dir=wallet_dir,
        wallet_location=wallet_dir,
        wallet_password=settings.adb_wallet_password,
        tcp_connect_timeout=20.0,
    )


def _ensure_user(cur, user: str, password: str) -> None:
    """ユーザーを冪等に用意し、パスワードを env と一致させる(ORA-01920=既存は無視)。"""
    try:
        cur.execute(f'CREATE USER {user} IDENTIFIED BY "{password}"')
    except oracledb.DatabaseError as e:
        if _ora_code(e) != 1920:  # ORA-01920: user name conflicts
            raise
        cur.execute(f'ALTER USER {user} IDENTIFIED BY "{password}"')


def _provision(settings: Settings) -> None:
    admin_pw = os.environ.get("ADB_ADMIN_PASSWORD", "")
    app_user, qry_user = settings.adb_user, settings.adb_query_user
    app_pw, qry_pw = settings.adb_password, settings.adb_query_password
    if not (admin_pw and app_pw and qry_pw and settings.adb_dsn):
        logger.warning("bootstrap skipped: ADB_ADMIN_PASSWORD / passwords / dsn が未設定")
        return

    wallet = _wallet_dir(settings)
    region = settings.oci_region
    acl_hosts = [
        f"inference.generativeai.{region}.oci.oraclecloud.com",
        f"generativeai.{region}.oci.oraclecloud.com",
        f"objectstorage.{region}.oraclecloud.com",
    ]

    conn = _admin_conn(settings, wallet, admin_pw)
    try:
        cur = conn.cursor()
        # アプリスキーマ(CREATE TABLE/VIEW + データセットのSelect AI実行に必要なDBMS_CLOUD系)
        _ensure_user(cur, app_user, app_pw)
        cur.execute(f"GRANT CREATE SESSION, RESOURCE, CREATE VIEW TO {app_user}")
        cur.execute(f"ALTER USER {app_user} QUOTA UNLIMITED ON DATA")
        for pkg in _DBMS_CLOUD_PKGS:
            cur.execute(f"GRANT EXECUTE ON {pkg} TO {app_user}")
        for host in acl_hosts:
            cur.execute(
                """
                BEGIN
                  DBMS_NETWORK_ACL_ADMIN.APPEND_HOST_ACE(
                    host => :h,
                    ace  => xs$ace_type(privilege_list => xs$name_list('http'),
                                        principal_name => :p,
                                        principal_type => xs_acl.ptype_db));
                END;""",
                h=host,
                p=app_user,
            )
        # 読取専用ユーザー(CREATE SESSIONのみ。datasetsが個別表にSELECTを付与)
        _ensure_user(cur, qry_user, qry_pw)
        cur.execute(f"GRANT CREATE SESSION TO {qry_user}")
        # Select AI のクレデンシャル: APIキー版JETUSE_OCI_CREDはRP不可。
        # リソースプリンシパルを有効化し OCI$RESOURCE_PRINCIPAL を使えるようにする(best-effort)。
        try:
            cur.execute(
                "BEGIN DBMS_CLOUD_ADMIN.ENABLE_RESOURCE_PRINCIPAL(username => :u); END;",
                u=app_user,
            )
            logger.info("resource principal enabled for %s", app_user)
        except oracledb.DatabaseError:
            logger.warning(
                "ENABLE_RESOURCE_PRINCIPAL 失敗(Select AIは手動クレデンシャルが必要)",
                exc_info=True,
            )
        conn.commit()
    finally:
        conn.close()
    logger.info("schema provisioned: %s / %s (+grants, ACL)", app_user, qry_user)


def bootstrap() -> None:
    """ADB ACTIVE まで待ってスキーマを用意し、マイグレーションを適用する。"""
    settings = get_settings()
    deadline = time.monotonic() + BOOTSTRAP_TIMEOUT_S
    while True:
        try:
            _provision(settings)
            break
        except Exception:  # noqa: BLE001
            if time.monotonic() >= deadline:
                logger.exception("bootstrap がタイムアウト。API起動は継続(DB系は503)")
                return
            logger.info("ADB 未準備のため %ss 後に再試行", RETRY_INTERVAL_S)
            time.sleep(RETRY_INTERVAL_S)

    try:
        from .migrate import migrate

        applied = migrate()
        logger.info("migrations applied: %s", applied or "(up to date)")
    except Exception:  # noqa: BLE001
        logger.exception("migrate 失敗(API起動は継続。解消までDB系は503)")

    post_migrate_maintenance()


def post_migrate_maintenance() -> None:
    """SP2-02(specs/18): 承認済み定義の冪等再適用 + 起動時 reconcile。

    各ステップは best-effort でログを残す — 失敗しても API は起動するが、
    対応する経路は各ゲート(vpd.integrity_gate / owner_key_gate / upload_gate)が
    fail-closed(503)に保つ。ここでは権限付与は行わない(初回セットアップは人間ゲート)。
    """
    from . import owner_keys, rag, rag_ledger, vpd

    try:
        vpd.reapply_definitions()
        logger.info("vpd/lock approved definitions reapplied")
    except Exception:  # noqa: BLE001
        logger.exception("vpd definitions reapply 失敗(dbchat/datasets は 503 のまま)")
    try:
        problems = vpd.verify_integrity()
        if problems:
            logger.error("VPD integrity problems: %s", "; ".join(problems)[:500])
    except Exception:  # noqa: BLE001
        logger.exception("VPD integrity verify 失敗")
    try:
        owner_keys.owner_key_gate()
    except owner_keys.OwnerKeyPreflightError:
        logger.error("owner key preflight: 予約接頭辞行が未分類(該当経路は 503)")
    except Exception:  # noqa: BLE001
        logger.exception("owner key preflight 失敗")
    try:
        summary = rag_ledger.reconcile(
            # locator ごとの project を走査(region/project 変更後も旧 File を辿る)
            lambda loc=None: rag.list_all_external_files(rag._dp_for(loc)),
            lambda ext_id, loc=None: rag.delete_external_file(ext_id, rag._dp_for(loc)),
            lambda ok, rid, ext, loc=None: rag.delete_original_exact(
                ok, rid, ext, locator=loc),
            _recover_confirmed,
        )
        logger.info("rag ledger reconcile: %s", summary)
    except Exception:  # noqa: BLE001
        logger.exception("rag ledger reconcile 失敗(後で再実行可)")
        try:
            rag_ledger.close_upload_gate()  # reconcile 未完なら upload を fail-closed に
        except Exception:  # noqa: BLE001 — DB 未到達なら gate は前回永続値を保持する
            logger.exception("close_upload_gate 失敗(DB 未到達 — gate は前回値保持)")


def _recover_confirmed(row: dict, has_file: bool) -> None:
    """confirmed 行の回復マトリクス(specs/18 §3.1)。

    (rag_files 行あり, File あり)=正常 / (行あり, File なし)=幽霊 → 個別削除手順で整合回収 /
    (行なし, File あり)=File・原本を削除して解放 / (行なし, File なし)=解放のみ。
    """
    from . import rag, rag_ledger
    from .db import connect

    loc = row.get("locator") or None  # 行 locator で旧 project の File/原本も辿る(B002)
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT owner_sub FROM rag_files WHERE id = :id", id=row["id"])
        db_row = cur.fetchone()
    if db_row and has_file:
        return  # 正常
    if db_row:
        # 幽霊(行あり・File なし): 個別削除手順(外部先行)で整合回収。delete_file は
        # ledger の write-ahead locator を内部で引くため locator の再指定は不要
        rag.delete_file(db_row[0], row["id"])
        return
    if has_file:
        rag.delete_external_file(row["external_file_id"], rag._dp_for(loc))
    rag.delete_original_exact(row["owner_key"], row["id"], row["ext"], locator=loc)
    rag_ledger.release(row["id"])


if __name__ == "__main__":
    from .logging import configure

    configure()
    bootstrap()
