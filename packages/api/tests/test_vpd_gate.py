"""VPD 完全性ゲート(fail-closed)とコンテキスト set/clear 契約(specs/18 §4.3)。"""

import pytest

from jetuse_core import nl2sql, owner_keys, vpd


@pytest.fixture(autouse=True)
def reset_gate(monkeypatch):
    vpd._integrity_ok = False
    monkeypatch.setattr(owner_keys, "owner_key_gate", lambda: None)  # 移行ゲートは別テスト
    yield
    vpd._integrity_ok = False


def test_verify_integrity_noop_when_vpd_disabled(monkeypatch):
    """B004: VPD 無効(既定 = Public/main 互換)なら completeness 検証は健全([])。DB を触らない
    (未配備の従来環境で dbchat/datasets が恒久 503 になるのを防ぐ)。"""
    monkeypatch.setattr(vpd.get_settings(), "vpd_enabled", False)
    monkeypatch.setattr(vpd, "connect",
                        lambda: (_ for _ in ()).throw(AssertionError("DB を触るべきでない")))
    assert vpd.verify_integrity() == []
    vpd.integrity_gate()  # ゲートも通過(例外なし)


class _RecCur:
    def __init__(self):
        self.ddls: list[str] = []

    def execute(self, sql, **kw):
        self.ddls.append(sql)


class _RecConn:
    def __init__(self, cur):
        self._cur = cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self._cur

    def commit(self):
        pass


def _reapply_with(monkeypatch, vpd_enabled):
    cur = _RecCur()
    monkeypatch.setattr(vpd, "connect", lambda: _RecConn(cur))
    monkeypatch.setattr(vpd.get_settings(), "vpd_enabled", vpd_enabled)
    vpd.reapply_definitions()
    return cur.ddls


def test_reapply_creates_lock_package_even_when_vpd_disabled(monkeypatch):
    """review-13 B001: JETUSE_LOCK cover package は VPD 無効でも作る(Public でも demo 排他リースが
    必要)。VPD 固有の context/policy/setter GRANT は vpd_enabled のときだけ。"""
    ddls = _reapply_with(monkeypatch, False)
    joined = "\n".join(ddls)
    assert any("PACKAGE JETUSE_LOCK" in d for d in ddls)       # lock は作る
    assert any("PACKAGE BODY JETUSE_LOCK" in d for d in ddls)
    assert "CREATE OR REPLACE CONTEXT" not in joined           # VPD 固有は作らない
    assert "DBMS_RLS" not in joined and "GRANT EXECUTE" not in joined


def test_reapply_creates_vpd_and_lock_when_enabled(monkeypatch):
    ddls = _reapply_with(monkeypatch, True)
    joined = "\n".join(ddls)
    assert any("PACKAGE JETUSE_LOCK" in d for d in ddls)       # lock も
    assert "CREATE OR REPLACE CONTEXT" in joined               # VPD context も
    assert "GRANT EXECUTE" in joined                            # query user への setter GRANT


def test_integrity_gate_fails_closed_until_verified(monkeypatch):
    monkeypatch.setattr(vpd, "verify_integrity", lambda: ["T1: VPD policy missing"])
    with pytest.raises(vpd.DatasetsSecurityError):
        vpd.integrity_gate()
    # 問題解消後は再起動なしで解除(否定は毎回再検証)
    monkeypatch.setattr(vpd, "verify_integrity", lambda: [])
    vpd.integrity_gate()
    # 肯定はプロセス内キャッシュ(以後 verify を呼ばない)
    monkeypatch.setattr(vpd, "verify_integrity",
                        lambda: (_ for _ in ()).throw(AssertionError("cached")))
    vpd.integrity_gate()


def test_datasets_routes_return_503_when_gate_closed(monkeypatch):
    from fastapi.testclient import TestClient

    from service.main import app

    monkeypatch.setattr(vpd, "verify_integrity",
                        lambda: ["JETUSE_DS_X: registry rows = 0 (期待 1)"])
    client = TestClient(app)
    res = client.get("/api/db/datasets")
    assert res.status_code == 503
    assert "fail-closed" in res.json()["detail"]


class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def callproc(self, name, args):
        self.conn.calls.append((name.split(".")[-1], tuple(args)))
        if self.conn.fail_on and name.endswith(self.conn.fail_on):
            raise RuntimeError(f"{self.conn.fail_on} failed")

    def execute(self, sql, **binds):
        self.conn.calls.append(("execute", sql))

    def fetchmany(self, n):
        return []

    @property
    def description(self):
        return [("C",)]


class _Conn:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on
        self.call_timeout = 0

    def cursor(self):
        return _Cur(self)


class _Pool:
    def __init__(self, conn):
        self.conn = conn
        self.dropped = []
        self.released = []

    def acquire(self):
        return self.conn

    def drop(self, conn):
        self.dropped.append(conn)

    def release(self, conn):
        conn.calls.append(("release", ()))
        self.released.append(conn)


def test_execute_readonly_sets_and_clears_context(monkeypatch):
    monkeypatch.setattr(vpd, "verify_integrity", lambda: [])  # ゲートは別テストで検証
    monkeypatch.setattr(vpd.get_settings(), "vpd_enabled", True)  # VPD 有効経路を検証
    conn = _Conn()
    pool = _Pool(conn)
    monkeypatch.setattr(nl2sql, "_get_query_pool", lambda: pool)
    nl2sql.execute_readonly("SELECT 1 FROM dual", owner_key="user-a")
    names = [c[0] for c in conn.calls]
    # parse 前に set → 実行 → finally で clear → 返却
    assert names.index("set_owner") < names.index("execute")
    assert names.index("execute") < names.index("clear_owner")
    assert names[-1] == "release"
    assert ("set_owner", ("user-a",)) in conn.calls
    assert pool.dropped == []


def test_execute_readonly_set_failure_prevents_sql(monkeypatch):
    monkeypatch.setattr(vpd, "verify_integrity", lambda: [])
    monkeypatch.setattr(vpd.get_settings(), "vpd_enabled", True)
    conn = _Conn(fail_on="set_owner")
    monkeypatch.setattr(nl2sql, "_get_query_pool", lambda: _Pool(conn))
    with pytest.raises(RuntimeError):
        nl2sql.execute_readonly("SELECT 1 FROM dual", owner_key="user-a")
    assert ("execute", "SELECT 1 FROM dual") not in conn.calls  # SQL を実行しない


def test_execute_readonly_clear_failure_drops_connection(monkeypatch):
    """clear に失敗した接続は再利用しない(コンテキスト残留の越境防止)。"""
    monkeypatch.setattr(vpd, "verify_integrity", lambda: [])
    monkeypatch.setattr(vpd.get_settings(), "vpd_enabled", True)
    conn = _Conn(fail_on="clear_owner")
    pool = _Pool(conn)
    monkeypatch.setattr(nl2sql, "_get_query_pool", lambda: pool)
    out = nl2sql.execute_readonly("SELECT 1 FROM dual", owner_key="user-a")
    assert out["columns"] == ["C"]
    assert pool.dropped == [conn]  # プールへ返さず破棄


def test_execute_readonly_without_owner_skips_context(monkeypatch):
    """owner なし(SH 等の固定スキーマ照会)はコンテキストを設定しない。
    dataset 表は VPD の default-deny が必ず 0 行を返す(層1 — 実 ADB E2E で検証)。"""
    monkeypatch.setattr(vpd, "verify_integrity", lambda: [])
    conn = _Conn()
    monkeypatch.setattr(nl2sql, "_get_query_pool", lambda: _Pool(conn))
    nl2sql.execute_readonly("SELECT 1 FROM dual", None)  # owner なしモード(層2は全 DS 拒否)
    assert all(c[0] != "set_owner" for c in conn.calls)
