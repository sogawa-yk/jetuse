"""ADB接続(CHAT-02)。mTLSウォレットを非公開バケットから取得して接続プールを作る。

CHAT-07: DB停止時に無限ハングしないよう、接続確立・プール取得・SQL往復の
3層すべてにタイムアウトを設定する(夜間停止でADBがSTOPPEDのままの実障害対策)。
"""

import base64
import contextlib
import io
import logging
import os
import pathlib
import threading
import zipfile
from collections.abc import Iterator

import oracledb

from .settings import Settings, get_settings

logger = logging.getLogger("jetuse.db")

oracledb.defaults.fetch_lobs = False  # CLOBをstrで受ける

_pool: oracledb.ConnectionPool | None = None
_lock = threading.Lock()

WALLET_CACHE = "/tmp/adb_wallet"
# SQL往復の上限(ms)。超過でDPY-4011/ORA-3136相当が上がり503に変換される
CALL_TIMEOUT_MS = int(os.environ.get("DB_CALL_TIMEOUT_MS", "10000"))


def _rp_signer():
    import oci

    return oci.auth.signers.get_resource_principals_signer()


def _wallet_bytes(settings: Settings) -> bytes:
    """ウォレットzipのバイト列を取得。バケット優先、無ければADB OCIDからAPI生成(INFRA-03)。"""
    import oci

    rp = os.environ.get("AUTH_MODE") == "resource_principal"
    if settings.adb_wallet_bucket:
        if rp:
            client = oci.object_storage.ObjectStorageClient(
                {"region": settings.oci_region}, signer=_rp_signer()
            )
        else:
            from .genai import load_local_oci_config

            client = oci.object_storage.ObjectStorageClient(load_local_oci_config())
        ns = client.get_namespace().data
        obj = client.get_object(ns, settings.adb_wallet_bucket, settings.adb_wallet_object)
        content = obj.data.content
        # Terraformが base64 テキストで配置したウォレットはデコードして使う(INFRA-03)
        return base64.b64decode(content) if settings.adb_wallet_base64 else content
    if settings.adb_ocid:
        # ORMワンクリック: Database APIでウォレットを直接生成(リソースプリンシパル)
        if rp:
            db = oci.database.DatabaseClient({"region": settings.oci_region}, signer=_rp_signer())
        else:
            from .genai import load_local_oci_config

            db = oci.database.DatabaseClient(load_local_oci_config())
        resp = db.generate_autonomous_database_wallet(
            settings.adb_ocid,
            oci.database.models.GenerateAutonomousDatabaseWalletDetails(
                generate_type="SINGLE", password=settings.adb_wallet_password
            ),
        )
        return resp.data.content
    raise RuntimeError("no wallet source: set adb_wallet_bucket or adb_ocid")


def _fetch_wallet(settings: Settings) -> str:
    """ウォレットzipを取得して展開(バケット or ADB生成。リソースプリンシパル対応)。"""
    dest = pathlib.Path(WALLET_CACHE)
    if (dest / "tnsnames.ora").exists():
        return str(dest)
    dest.mkdir(parents=True, exist_ok=True)
    zipfile.ZipFile(io.BytesIO(_wallet_bytes(settings))).extractall(dest)
    logger.info("ADB wallet ready at %s", dest)
    return str(dest)


def _wallet_dir(settings: Settings) -> str:
    if settings.adb_wallet_dir:
        return settings.adb_wallet_dir
    return _fetch_wallet(settings)


def get_pool() -> oracledb.ConnectionPool:
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                s = get_settings()
                try:
                    wd = _wallet_dir(s)
                except Exception as e:
                    # ウォレット取得失敗もDB利用不可として503系に正規化(CHAT-07)
                    raise oracledb.OperationalError(f"db init failed: {e}") from e
                _pool = oracledb.create_pool(
                    user=s.adb_user,
                    password=s.adb_password,
                    dsn=s.adb_dsn,
                    config_dir=wd,
                    wallet_location=wd,
                    wallet_password=s.adb_wallet_password,
                    min=1,
                    max=4,
                    tcp_connect_timeout=5.0,
                    getmode=oracledb.POOL_GETMODE_TIMEDWAIT,
                    # コールド接続のmTLS確立は5秒を超える(実測: 移行ツールがDPY-4005)。
                    # DB停止検知の速さはtcp_connect_timeout側が担うため15秒で安全側に
                    wait_timeout=15000,
                    ping_interval=30,
                )
    return _pool


@contextlib.contextmanager
def connect() -> Iterator[oracledb.Connection]:
    """call_timeout付きのプール接続。リポジトリ層はこれを使う(CHAT-07)。"""
    with get_pool().acquire() as conn:
        conn.call_timeout = CALL_TIMEOUT_MS
        yield conn
