"""排他リースの実装契約(specs/18 §3.2.1 — DBMS_LOCK cover package)。

fake プール/カーソルで REQUEST の全戻り値処理・RELEASE 異常時の接続破棄・
再入契約(require_lease_for)・mutation の status 再確認を検証する。
commit 跨ぎ保持・セッション死解放・実待機は実 ADB E2E(feasibility/scenario)で検証済み。
"""

import pytest

import jetuse_core.demos as demos_repo
from jetuse_core import demo_lease
from jetuse_core.demo_lease import (
    DemoGoneError,
    DemoLease,
    LeaseContractError,
    LeaseTimeoutError,
    LeaseUnavailableError,
    require_lease_for,
)


class FakeCursor:
    def __init__(self, conn, request_rcs, release_rc):
        self.conn = conn
        self.request_rcs = request_rcs
        self.release_rc = release_rc

    def execute(self, sql, **binds):
        self.sql = sql

    def callfunc(self, name, rtype, args):
        if name == "jetuse_lock.allocate_unique":
            return "000000000000AB12"  # ALLOCATE_UNIQUE のハンドル(決定値)
        if name == "jetuse_lock.request":
            rc = self.request_rcs.pop(0)
            if isinstance(rc, Exception):
                raise rc
            self.conn.held = (rc == 0)
            return rc
        if name == "jetuse_lock.release":
            rc = self.release_rc
            if isinstance(rc, Exception):
                raise rc
            if rc == 0:
                self.conn.held = False
            return rc
        raise AssertionError(name)


class FakeConn:
    def __init__(self, request_rcs, release_rc=0):
        self.call_timeout = 10000
        self.held = False
        self._cur = FakeCursor(self, list(request_rcs), release_rc)

    def cursor(self):
        return self._cur


class FakePool:
    def __init__(self, conn):
        self.conn = conn
        self.dropped = []
        self.released = []

    def acquire(self):
        return self.conn

    def drop(self, conn):
        self.dropped.append(conn)

    def release(self, conn):
        self.released.append(conn)


@pytest.fixture()
def pool(monkeypatch):
    holder = {}

    def install(request_rcs, release_rc=0):
        conn = FakeConn(request_rcs, release_rc)
        p = FakePool(conn)
        holder["pool"] = p
        monkeypatch.setattr(demo_lease, "_get_lease_pool", lambda: p)
        return p

    return install


def test_acquire_success_sets_and_restores_call_timeout(pool):
    p = pool([0])
    with demo_lease.acquire("d1") as lease:
        assert lease.demo_id == "d1"
        # リース待ちを跨ぐ呼び出し上限はミリ秒(310_000)。専用セッションにのみ設定
        assert p.conn.call_timeout == demo_lease.LEASE_CALL_TIMEOUT_MS
    assert p.released == [p.conn] and p.dropped == []
    assert p.conn.call_timeout == 10000  # 返却時に既定へ復元
    assert p.conn.held is False  # RELEASE 済み


def test_request_timeout_maps_to_lease_timeout(pool):
    p = pool([1])
    with pytest.raises(LeaseTimeoutError), demo_lease.acquire("d1"):
        pass
    assert p.released == [p.conn]  # 取得していないので RELEASE 不要・返却のみ


@pytest.mark.parametrize("rc", [2, 3, 5])
def test_request_abnormal_rcs_are_unavailable_and_dropped(pool, rc):
    p = pool([rc])
    with pytest.raises(LeaseUnavailableError), demo_lease.acquire("d1"):
        pass
    assert p.dropped == [p.conn]


def test_request_rc4_stale_session_is_dropped_not_adopted(pool):
    """rc=4(既保持)はプール接続のロック残留 = 新規取得成功と誤認しない。"""
    p = pool([4])
    with pytest.raises(LeaseUnavailableError), demo_lease.acquire("d1"):
        pass
    assert p.dropped == [p.conn]


def test_release_nonzero_drops_connection(pool):
    p = pool([0], release_rc=3)
    with demo_lease.acquire("d1"):
        pass
    assert p.dropped == [p.conn] and p.released == []


def test_release_exception_drops_connection(pool):
    p = pool([0], release_rc=RuntimeError("session dead"))
    with demo_lease.acquire("d1"):
        pass
    assert p.dropped == [p.conn]


def test_cover_package_missing_is_fail_closed(pool):
    import oracledb

    p = pool([oracledb.DatabaseError("PLS-00201: identifier 'JETUSE_LOCK' must be declared")])
    with pytest.raises(LeaseUnavailableError), demo_lease.acquire("d1"):
        pass
    assert p.dropped == [p.conn]


def test_pool_exhaustion_is_503(monkeypatch):
    import oracledb

    class ExhaustedPool:
        def acquire(self):
            raise oracledb.DatabaseError("DPY-4005: timed out waiting for pool")

    monkeypatch.setattr(demo_lease, "_get_lease_pool", lambda: ExhaustedPool())
    with pytest.raises(LeaseUnavailableError), demo_lease.acquire("d1"):
        pass


def test_mutation_rechecks_status_under_lease(pool, monkeypatch):
    """mutation 取得 = 行なし/deleting は 404(specs/18 §3.2.1 の 2 契約の片方)。"""
    pool([0, 0, 0])
    monkeypatch.setattr(demos_repo, "get_demo", lambda i: None)
    with pytest.raises(DemoGoneError), demo_lease.mutation("d1"):
        pass
    monkeypatch.setattr(demos_repo, "get_demo",
                        lambda i: {"id": i, "status": "deleting", "owner_sub": "u"})
    with pytest.raises(DemoGoneError), demo_lease.mutation("d1"):
        pass
    monkeypatch.setattr(demos_repo, "get_demo",
                        lambda i: {"id": i, "status": "ready", "owner_sub": "u"})
    with demo_lease.mutation("d1") as lease:
        assert lease.demo_id == "d1"


def test_require_lease_for_contract():
    """再入契約: demo namespace への書き込みは保持トークンの検証のみ(再取得しない)。"""
    lease = DemoLease(demo_id="d1", _conn=None)
    require_lease_for("demo_d1", lease)  # 一致 → OK
    require_lease_for("user-x", None)    # user 経路はリース対象外
    with pytest.raises(LeaseContractError):
        require_lease_for("demo_d1", None)  # 未保持
    with pytest.raises(LeaseContractError):
        require_lease_for("demo_d2", lease)  # 別 demo のトークン
    lease.released = True
    with pytest.raises(LeaseContractError):
        require_lease_for("demo_d1", lease)  # 解放済みトークン
