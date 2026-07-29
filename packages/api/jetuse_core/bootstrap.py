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

import json
import logging
import os
import threading
import time

import oracledb

from .db import _wallet_dir
from .settings import Settings, get_settings

logger = logging.getLogger("jetuse.bootstrap")

# Select AI(データセット)クレデンシャルの可視化(PORT-02)。bootstrap は best-effort で
# ENABLE_RESOURCE_PRINCIPAL を試みる(下記 _provision)。/api/health がこの結果を読む。
# 初期値はok=None(未検証) — bootstrap未実行/未完了を「成功」と偽って見せない
# (レビュー指摘F-003: 既定trueだとRUN_DB_BOOTSTRAP未設定や起動直後にhealthが誤ってokを返す)。
_rp_lock = threading.Lock()
_rp_status: dict = {
    "ok": None,
    "hint": "起動時のENABLE_RESOURCE_PRINCIPAL検証が未実行です(bootstrap未完了)",
}

_RP_HINT = (
    "ENABLE_RESOURCE_PRINCIPAL に失敗しました。Select AI(データセット)のクレデンシャルが"
    "使えない可能性があります。動的グループへの generative-ai-family 権限、および"
    "Object Storage バケットの read 権限を確認してください"
)


# bootstrap は entrypoint.sh から**別プロセス**で起動される(`python -m jetuse_core.bootstrap &`)
# ため、プロセス内のグローバル変数だけでは uvicorn 側の /api/health から見えず、配備が
# 正常でも dbchat が恒久的に "unavailable"(select_ai.ok=null)と表示されてしまう。
# 同一コンテナのファイルシステム経由で結果を渡す(2026-07-28 実機で誤判定を確認)。
_RP_STATUS_FILE = os.environ.get("RP_STATUS_FILE", "/tmp/jetuse-rp-status.json")  # noqa: S108


def resource_principal_status() -> dict:
    with _rp_lock:
        if _rp_status.get("ok") is not None:
            return dict(_rp_status)
    try:
        with open(_RP_STATUS_FILE, encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict) and "ok" in loaded:
            return loaded
    except (OSError, ValueError):
        pass
    with _rp_lock:
        return dict(_rp_status)


def _set_resource_principal_status(ok: bool | None, hint: str | None = None) -> None:
    global _rp_status
    status: dict = {"ok": ok, **({"hint": hint} if hint else {})}
    with _rp_lock:
        _rp_status = status
    # 直接 open("w") で書くと truncate 済み・未書き込みの瞬間に uvicorn 側が壊れたJSONを
    # 読みうる。同一ディレクトリの一時ファイル → os.replace で原子的に差し替える。
    tmp = f"{_RP_STATUS_FILE}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _RP_STATUS_FILE)
    except OSError:  # 書けなくても bootstrap 本体は続行する(health の精度だけが落ちる)
        logger.warning("resource principal status を %s へ書けませんでした", _RP_STATUS_FILE)
        try:
            os.unlink(tmp)
        except OSError:
            pass

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


def _ensure_user(cur, user: str, password: str) -> bool:
    """ユーザーを冪等に用意し、パスワードを env と一致させる(ORA-01920=既存は無視)。

    戻り値: パスワード再利用制限(ORA-28007)で ALTER を諦めた場合 True
    (呼び出し側が実ログインで裏取りする)。
    """
    try:
        cur.execute(f'CREATE USER {user} IDENTIFIED BY "{password}"')
    except oracledb.DatabaseError as e:
        if _ora_code(e) != 1920:  # ORA-01920: user name conflicts
            raise
        try:
            cur.execute(f'ALTER USER {user} IDENTIFIED BY "{password}"')
        except oracledb.DatabaseError as e2:
            # ORA-28007: 同じパスワードは再設定できない(プロファイルのパスワード再利用制限)。
            # Terraform 生成のパスワードは state に固定されているため、**コンテナが再起動する
            # たび**(イメージ更新・CI再作成・クラッシュ復帰)に同じ値で ALTER しようとして必ず
            # ここに来る。これを失敗扱いにすると bootstrap 全体が 25 分リトライして諦め、
            # migrate() まで実行されない(2026-07-28 実機で再現 — FIX-58)。
            if _ora_code(e2) != 28007:
                raise
            logger.info("%s は既存でパスワード再設定不可(ORA-28007) — ログインで確認する", user)
            return True
    return False


def _verify_login(settings: Settings, wallet_dir: str, user: str, password: str) -> None:
    """ORA-28007 で ALTER を諦めた場合の裏取り。目的のパスワードで実際に入れるか確認する。

    ORA-28007 は「履歴にある」という意味で「現在のパスワードと同じ」とは限らない
    (誰かが手動で変えた等)。無検証で通すと後段のDB接続が原因不明で落ちるため確かめる。
    """
    oracledb.connect(
        user=user,
        password=password,
        dsn=settings.adb_dsn,
        config_dir=wallet_dir,
        wallet_location=wallet_dir,
        wallet_password=settings.adb_wallet_password,
        tcp_connect_timeout=20.0,
    ).close()


def _provision(settings: Settings) -> None:
    admin_pw = os.environ.get("ADB_ADMIN_PASSWORD", "")
    app_user, qry_user = settings.adb_user, settings.adb_query_user
    app_pw, qry_pw = settings.adb_password, settings.adb_query_password
    if not (admin_pw and app_pw and qry_pw and settings.adb_dsn):
        hint = (
            "bootstrap をスキップしました(ADB_ADMIN_PASSWORD / ADB_PASSWORD / "
            "ADB_QUERY_PASSWORD / ADB_DSN のいずれかが未設定)。Select AI は未検証です"
        )
        logger.warning(hint)
        # ここで抜けると bootstrap は「成功」としてループを出るため、起動時に書いた
        # ok=None(実行中)のままになり health が永久に「実行中」と表示される(F005)。
        _set_resource_principal_status(False, hint)
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
        reuse_blocked = {app_user: _ensure_user(cur, app_user, app_pw)}
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
        reuse_blocked[qry_user] = _ensure_user(cur, qry_user, qry_pw)
        cur.execute(f"GRANT CREATE SESSION TO {qry_user}")
        conn.commit()
        # ORA-28007 でパスワード同期を諦めたユーザーは、実際に入れるか裏取りする
        # (失敗すればリトライループへ送り、原因つきでログに残す)
        for user, pw in ((app_user, app_pw), (qry_user, qry_pw)):
            if reuse_blocked.get(user):
                _verify_login(settings, wallet, user, pw)
        # Select AI のクレデンシャル: APIキー版JETUSE_OCI_CREDはRP不可。
        # リソースプリンシパルを有効化し OCI$RESOURCE_PRINCIPAL を使えるようにする(best-effort)。
        try:
            cur.execute(
                "BEGIN DBMS_CLOUD_ADMIN.ENABLE_RESOURCE_PRINCIPAL(username => :u); END;",
                u=app_user,
            )
            logger.info("resource principal enabled for %s", app_user)
            _set_resource_principal_status(True)
        except oracledb.DatabaseError as e:
            logger.warning("%s: %s", _RP_HINT, e, exc_info=True)
            _set_resource_principal_status(False, _RP_HINT)
        conn.commit()
    finally:
        conn.close()
    logger.info("schema provisioned: %s / %s (+grants, ACL)", app_user, qry_user)


def bootstrap() -> None:
    """ADB ACTIVE まで待ってスキーマを用意し、マイグレーションを適用する。"""
    settings = get_settings()
    # 前回起動が残した ok=true を今回の起動の結果として読ませない(コンテナ再起動で
    # bootstrap が恒久的に失敗しても health が「Select AI 利用可」と偽り続ける — F-004)。
    # 起動のたびに「未検証」へ戻し、結論が出た時点で上書きする。
    _set_resource_principal_status(None, "bootstrap 実行中(ENABLE_RESOURCE_PRINCIPAL 未検証)")
    deadline = time.monotonic() + BOOTSTRAP_TIMEOUT_S
    while True:
        try:
            _provision(settings)
            break
        except Exception as e:  # noqa: BLE001
            if time.monotonic() >= deadline:
                logger.exception("bootstrap がタイムアウト。API起動は継続(DB系は503)")
                _set_resource_principal_status(
                    False,
                    f"bootstrap がタイムアウトしました({type(e).__name__}: {e})。"
                    "ADB の状態と ADB_ADMIN_PASSWORD / ウォレット / 権限を確認してください",
                )
                return
            # 例外を出さずに「ADB 未準備」とだけ書くと、恒久的な失敗(権限不足・ORA-*)が
            # プロビジョニング待ちと見分けられず 25 分間埋もれる(FIX-58)。理由を必ず残す。
            logger.info(
                "bootstrap 再試行(%ss後): %s: %s", RETRY_INTERVAL_S, type(e).__name__, e
            )
            time.sleep(RETRY_INTERVAL_S)

    try:
        from .migrate import migrate

        applied = migrate()
        logger.info("migrations applied: %s", applied or "(up to date)")
    except Exception:  # noqa: BLE001
        logger.exception("migrate 失敗(API起動は継続。解消までDB系は503)")


if __name__ == "__main__":
    from .logging import configure

    configure()
    bootstrap()
