"""demo 単位の排他リース(specs/18 §3.2.1 — DBMS_LOCK cover package・Gate 2 最小案)。

- 専用 DB セッションで JETUSE_LOCK.ALLOCATE_UNIQUE(demo 名)→一意ハンドル→REQUEST(X モード)を
  取得して保持する。セッションスコープのため deleting 遷移の commit を跨いで保持できる
  (SELECT FOR UPDATE では不可 — 実機確認済み: runs/2026-07-06T1113_SP2-02/e2e/feasibility.md)。
  ALLOCATE_UNIQUE は同名→同ハンドルを全プロセスで保証するため ORA_HASH 数値 ID の衝突が無い。
- リース専用の小プール(既存作業プール〔最大4〕と分離)。上限到達は 503。
- DBMS_LOCK.REQUEST の timeout は「秒」、リース待ちを跨ぐ oracledb call_timeout は
  「ミリ秒」(リース専用セッションにのみ設定し、返却時に既定へ戻す)。
- REQUEST は例外でなく戻り値: 0=成功 / 4=既保持(プール接続のロック残留 = 異常) /
  1=timeout / 2=deadlock / 3=パラメタ / 5=不正ハンドル。全戻り値を処理する。
- RELEASE も戻り値 0 のみ成功。非 0 または例外時は接続をプールへ返さず破棄する
  (残留セッションロックが後続を待たせ、REQUEST=4 を新規取得成功と誤認するのを防ぐ)。
- 再入契約: リースは最外層だけが取得・解放し、内部関数へはトークン(DemoLease)を引数で
  伝播する。require_lease_for() が demo namespace への書き込みで保持を検証する。
- DBMS_LOCK / cover package が使えない環境では LeaseUnavailableError(ルート側 503)
  = fail-closed(specs/18 §3.2.1 — 弱い代替で残骸リスクを黙認しない)。

cover package は ADMIN 所有(definer's rights)= vpd.lock_definitions()。アプリスキーマへは
EXECUTE + private synonym のみ(vpd.provision_lock_for)で DBMS_LOCK 直付けはしない。ADMIN
セットアップ(provision)は人間ゲート(runs/<run-id>/e2e/APPROVAL-REQUEST.md → APPROVAL.md)。
"""

import contextlib
import logging
import threading
from dataclasses import dataclass, field

import oracledb

from .db import CALL_TIMEOUT_MS, _wallet_dir
from .settings import get_settings

logger = logging.getLogger("jetuse.demo_lease")

LOCK_TIMEOUT_S = 300           # DBMS_LOCK.REQUEST timeout(秒)
LEASE_CALL_TIMEOUT_MS = 310_000  # リース待ちを跨ぐ DB 呼び出し上限(ミリ秒)
LEASE_POOL_MAX = 4             # 同時 demo mutation の想定上限(超過は 503)

_lease_pool: oracledb.ConnectionPool | None = None
_pool_lock = threading.Lock()


class LeaseUnavailableError(Exception):
    """リース基盤が使えない(DBMS_LOCK 不可・プール枯渇・RELEASE 異常)。ルートは 503。"""


class LeaseTimeoutError(Exception):
    """リース取得 timeout(先行操作が保持中)。ルートは 503(再試行可)。"""


class LeaseContractError(Exception):
    """demo namespace への書き込みがリースを保持していない(実装契約違反)。"""


class DemoGoneError(Exception):
    """リース取得後の status 再確認で行なし/deleting(mutation 契約)。ルートは 404。"""


@dataclass
class DemoLease:
    """保持中リースのトークン。内部関数へ引数で伝播する(再取得しない)。"""

    demo_id: str
    _conn: oracledb.Connection = field(repr=False)
    released: bool = False


def _get_lease_pool() -> oracledb.ConnectionPool:
    """リース専用の小プール(作業プールと分離 — 保持中の枯渇を防ぐ)。"""
    global _lease_pool
    if _lease_pool is None:
        with _pool_lock:
            if _lease_pool is None:
                s = get_settings()
                wd = _wallet_dir(s)
                _lease_pool = oracledb.create_pool(
                    user=s.adb_user,
                    password=s.adb_password,
                    dsn=s.adb_dsn,
                    config_dir=wd,
                    wallet_location=wd,
                    wallet_password=s.adb_wallet_password,
                    min=0,
                    max=LEASE_POOL_MAX,
                    tcp_connect_timeout=5.0,
                    getmode=oracledb.POOL_GETMODE_TIMEDWAIT,
                    wait_timeout=5000,  # 上限到達は待たせず 503(専用プールの契約)
                    ping_interval=30,
                )
    return _lease_pool


def _lock_name_for(demo_id: str) -> str:
    """demo 名 → ALLOCATE_UNIQUE のロック名(全プロセスで同名 → 同ハンドル)。"""
    return "jetuse_demo_" + demo_id


@contextlib.contextmanager
def acquire(demo_id: str, *, timeout_s: int = LOCK_TIMEOUT_S):
    """demo 単位の排他リースを取得する(最外層専用)。

    取得後の status 再確認は呼び出し側の契約(mutation()/delete 側)が行う。
    """
    try:
        conn = _get_lease_pool().acquire()
    except oracledb.DatabaseError as e:
        raise LeaseUnavailableError(f"lease pool exhausted or unavailable: {e}") from e
    drop = False
    acquired = False
    handle = None
    try:
        conn.call_timeout = LEASE_CALL_TIMEOUT_MS
        cur = conn.cursor()
        try:
            handle = cur.callfunc(
                "jetuse_lock.allocate_unique", str, [_lock_name_for(demo_id)])
            rc = cur.callfunc("jetuse_lock.request", int, [handle, timeout_s])
        except oracledb.DatabaseError as e:
            # cover package/synonym なし・EXECUTE 未付与 = DBMS_LOCK 不可 → fail-closed(503)
            drop = True
            raise LeaseUnavailableError(
                f"demo lease infrastructure unavailable (JETUSE_LOCK): {e}"
            ) from e
        if rc == 1:
            raise LeaseTimeoutError(f"demo lease timeout after {timeout_s}s: {demo_id}")
        if rc == 4:
            # プール接続にロックが残留(前利用者の解放漏れ)。新規取得成功と誤認しない
            drop = True
            raise LeaseUnavailableError(f"stale lease on pooled session (rc=4): {demo_id}")
        if rc != 0:
            drop = True
            raise LeaseUnavailableError(f"DBMS_LOCK.REQUEST rc={rc}: {demo_id}")
        acquired = True
        lease = DemoLease(demo_id=demo_id, _conn=conn)
        try:
            yield lease
        finally:
            # 正常・異常を問わずトークンを無効化する(codex review-2 major — 本体が例外送出
            # した場合も DB ロックは下の finally で解放されるため、外部に残ったトークンを
            # 保持中と誤認させない。以後の require_lease_for が拒否する)。
            lease.released = True
    finally:
        if acquired:
            try:
                rc2 = conn.cursor().callfunc("jetuse_lock.release", int, [handle])
                if rc2 != 0:
                    drop = True
                    logger.error("lease RELEASE rc=%s (dropping connection): %s",
                                 rc2, demo_id)
            except Exception:
                drop = True
                logger.exception("lease RELEASE failed (dropping connection): %s", demo_id)
        try:
            if drop:
                _get_lease_pool().drop(conn)
            else:
                conn.call_timeout = CALL_TIMEOUT_MS  # 既定へ復元して返却
                _get_lease_pool().release(conn)
        except Exception:  # noqa: BLE001 — 返却失敗はログのみ(プールが自浄する)
            logger.exception("lease connection return failed")


@contextlib.contextmanager
def mutation(demo_id: str, *, timeout_s: int = LOCK_TIMEOUT_S):
    """mutation 取得(公開 API 既定): リース取得 → status 再確認(行なし/deleting は 404)。

    specs/18 §3.2.1 の 2 契約の片方。DELETE 側は demo_cleanup が allow_deleting 相当を実装。
    """
    from . import demos

    with acquire(demo_id, timeout_s=timeout_s) as lease:
        d = demos.get_demo(demo_id)
        if not d or d["status"] == "deleting":
            raise DemoGoneError(demo_id)
        yield lease


def require_lease_for(owner_key: str, lease: DemoLease | None) -> None:
    """demo namespace に外部リソース・DDL・登録行を作る操作の保持検証(再入契約)。

    user 経路(demo namespace でない owner)はリース対象外 = None で通る。
    """
    if not owner_key.startswith("demo_"):
        return
    if lease is None or lease.released or f"demo_{lease.demo_id}" != owner_key:
        raise LeaseContractError(
            f"demo namespace write without lease: {owner_key[:50]}"
        )
