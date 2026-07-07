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


def test_reapply_noop_when_vpd_disabled(monkeypatch):
    """Gate 2 最小案: JETUSE_LOCK は ADMIN provision へ移行。reapply は VPD 無効なら何もしない
    (app スキーマに lock を作らない — DB を触らず即 return)。"""
    ddls = _reapply_with(monkeypatch, False)
    assert ddls == []


def test_reapply_creates_vpd_only_when_enabled(monkeypatch):
    """VPD 有効時は VPD 固有定義 + setter GRANT のみ。lock はもう reapply では作らない。"""
    ddls = _reapply_with(monkeypatch, True)
    joined = "\n".join(ddls)
    assert "CREATE OR REPLACE CONTEXT" in joined               # VPD context
    assert "GRANT EXECUTE" in joined                            # query user への setter GRANT
    assert "PACKAGE JETUSE_LOCK" not in joined                 # lock は ADMIN provision へ移行


class _ProvCur:
    def __init__(self, existing_pkg=0, body_status="VALID", direct_grant=0):
        self.ddls: list[str] = []
        # 順に: SELECT USER→owner / body status / 既存 app package 数 / (移行時のみ)direct grant 数
        self._fetch = [("ADMIN",), (body_status,), (existing_pkg,), (direct_grant,)]

    def execute(self, sql, **kw):
        self.ddls.append(sql)

    def fetchone(self):
        return self._fetch.pop(0)


def test_provision_lock_for_admin_owned_minimal():
    """Gate 2 最小案: ADMIN 所有 JETUSE_LOCK(ALLOCATE_UNIQUE/REQUEST/RELEASE のみ)+ app への
    EXECUTE/synonym。app へ DBMS_LOCK 直付けはしない。"""
    cur = _ProvCur(existing_pkg=0)
    owner = vpd.provision_lock_for(cur, "APP_X")
    joined = "\n".join(cur.ddls)
    assert owner == "ADMIN"
    assert "PACKAGE JETUSE_LOCK" in joined and "PACKAGE BODY JETUSE_LOCK" in joined
    assert "ALLOCATE_UNIQUE" in joined                          # 最小 3 機能
    assert "GRANT EXECUTE ON ADMIN.JETUSE_LOCK TO APP_X" in joined
    assert "CREATE OR REPLACE SYNONYM APP_X.JETUSE_LOCK FOR ADMIN.JETUSE_LOCK" in joined
    assert "GRANT EXECUTE ON DBMS_LOCK" not in joined           # app へ直付けしない
    assert "DROP PACKAGE" not in joined                         # 既存 package なし → drop 不要
    assert "REVOKE" not in joined                               # 旧 direct grant なし → revoke 不要


def test_provision_lock_for_drops_shadowing_app_package():
    """保守ウィンドウ(app_offline=True)なら旧 app 所有 package を落として synonym 化する。"""
    cur = _ProvCur(existing_pkg=1)
    vpd.provision_lock_for(cur, "APP_X", app_offline=True)
    assert any("DROP PACKAGE APP_X.JETUSE_LOCK" in d for d in cur.ddls)


def test_provision_lock_for_refuses_live_migration_by_default():
    """旧 app 所有 package があり app_offline 未指定なら fail-closed で中断(numeric/named ロックは
    相互排他せず live 移行は排他を破る — review-17 blocker)。旧 package は落とさない。"""
    cur = _ProvCur(existing_pkg=1)
    with pytest.raises(RuntimeError, match="app_offline"):
        vpd.provision_lock_for(cur, "APP_X")
    assert not any("DROP PACKAGE" in d for d in cur.ddls)
    assert not any("SYNONYM" in d for d in cur.ddls)


def test_provision_lock_for_rejects_bad_identifier():
    """app_schema を DDL へ interpolate する前に識別子検証(注入防止 — review-17 major)。"""
    cur = _ProvCur()
    with pytest.raises(ValueError):
        vpd.provision_lock_for(cur, "APP_X; DROP USER VICTIM --")
    assert cur.ddls == []  # 検証前に DDL を1つも発行しない


class _AvailConn:
    def __init__(self, counts):
        self._counts = list(counts)  # 各 execute→fetchone で返す COUNT を順に

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self

    def execute(self, sql, **kw):
        pass

    def fetchone(self):
        return (self._counts.pop(0),)


def test_lock_available_true_when_synonym_present(monkeypatch):
    monkeypatch.setattr(vpd, "connect", lambda: _AvailConn([1]))  # synonym あり → 即 True
    assert vpd.lock_available() is True


def test_lock_available_false_when_absent(monkeypatch):
    """synonym も package も無い(Gate 2 provision 未実行)= 起動時に検知できる。"""
    monkeypatch.setattr(vpd, "connect", lambda: _AvailConn([0, 0]))
    assert vpd.lock_available() is False


def test_provision_lock_for_revokes_stale_direct_dbms_lock_on_migration():
    """移行時(旧 package あり + app_offline)に旧 SYS.DBMS_LOCK grant を REVOKE(least-priv)。"""
    cur = _ProvCur(existing_pkg=1, direct_grant=1)
    vpd.provision_lock_for(cur, "APP_X", app_offline=True)
    assert any("REVOKE EXECUTE ON SYS.DBMS_LOCK FROM APP_X" in d for d in cur.ddls)


def test_provision_lock_for_fresh_keeps_existing_grants():
    """fresh 構成(旧 package なし)は既存 grant を一切触らない — REVOKE しない(review-18 major)。"""
    cur = _ProvCur(existing_pkg=0, direct_grant=1)
    vpd.provision_lock_for(cur, "APP_X")
    assert not any("REVOKE" in d for d in cur.ddls)


def test_provision_lock_for_aborts_if_admin_body_invalid():
    """ADMIN body が不正コンパイル(DBMS_LOCK 権限不足)なら旧 app package を落とす前に中断
    (review-16 major — 稼働リースを壊さない)。"""
    cur = _ProvCur(existing_pkg=1, body_status="INVALID")
    with pytest.raises(RuntimeError):
        vpd.provision_lock_for(cur, "APP_X")
    assert not any("DROP PACKAGE" in d for d in cur.ddls)       # 旧 package を保持
    assert not any("SYNONYM" in d for d in cur.ddls)


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
