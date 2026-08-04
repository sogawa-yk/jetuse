"""DB自己ブートストラップ(INFRA-03)の冪等DDL/権限付与/RP有効化/migrate呼び出しの単体テスト。

実DBには接続せず oracledb.connect と _wallet_dir / migrate をモックする。
"""

import oracledb
import pytest

from jetuse_core import bootstrap
from jetuse_core.settings import Settings


class FakeErr:
    def __init__(self, code: int):
        self.code = code
        self.message = f"ORA-{code:05d}"


class FakeCursor:
    def __init__(
        self,
        fail_create_user: bool = False,
        fail_rp: bool = False,
        fail_alter_password_reuse: bool = False,
    ):
        self.executed: list[str] = []
        self._fail_create_user = fail_create_user
        self._fail_rp = fail_rp
        self._fail_alter_password_reuse = fail_alter_password_reuse

    def execute(self, sql, **kw):
        self.executed.append(sql.strip())
        if self._fail_create_user and sql.strip().startswith("CREATE USER "):
            raise oracledb.DatabaseError(FakeErr(1920))  # user already exists
        if self._fail_alter_password_reuse and sql.strip().startswith("ALTER USER ") \
                and "IDENTIFIED BY" in sql:
            raise oracledb.DatabaseError(FakeErr(28007))  # password cannot be reused
        if self._fail_rp and "ENABLE_RESOURCE_PRINCIPAL" in sql:
            raise oracledb.DatabaseError(FakeErr(20000))


class FakeConn:
    def __init__(self, cur: FakeCursor):
        self._cur = cur
        self.committed = False

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed = True

    def close(self):
        pass


def _settings() -> Settings:
    return Settings(
        adb_user="JETUSE_APP", adb_query_user="JETUSE_QUERY",
        adb_password="App#Pw1", adb_query_password="Qry#Pw1",
        adb_dsn="jetusedev_low", oci_region="ap-osaka-1",
        adb_wallet_password="w",
    )


@pytest.fixture
def patched(monkeypatch, tmp_path):
    monkeypatch.setenv("ADB_ADMIN_PASSWORD", "Admin#Pw1")
    monkeypatch.setattr(bootstrap, "_wallet_dir", lambda s: "/tmp/wallet")  # noqa: S108
    # RP状態はプロセス跨ぎ共有のためファイルに書かれる。共有/tmpを汚さないよう隔離する。
    monkeypatch.setattr(bootstrap, "_RP_STATUS_FILE", str(tmp_path / "rp-status.json"))
    return monkeypatch


def test_provision_creates_users_grants_acl_and_rp(patched):
    cur = FakeCursor()
    patched.setattr(oracledb, "connect", lambda **kw: FakeConn(cur))
    bootstrap._provision(_settings())
    joined = "\n".join(cur.executed)
    assert 'CREATE USER JETUSE_APP IDENTIFIED BY "App#Pw1"' in joined
    assert "GRANT CREATE SESSION, RESOURCE, CREATE VIEW TO JETUSE_APP" in joined
    assert "GRANT EXECUTE ON DBMS_CLOUD_AI TO JETUSE_APP" in joined
    assert "APPEND_HOST_ACE" in joined  # ネットワークACL
    assert 'CREATE USER JETUSE_QUERY IDENTIFIED BY "Qry#Pw1"' in joined
    assert "GRANT CREATE SESSION TO JETUSE_QUERY" in joined
    assert "ENABLE_RESOURCE_PRINCIPAL" in joined  # Select AI 用 RP 有効化(best-effort)


def test_provision_idempotent_when_user_exists(patched):
    # CREATE USER が ORA-01920 を投げても ALTER USER で冪等にパスワード同期
    cur = FakeCursor(fail_create_user=True)
    patched.setattr(oracledb, "connect", lambda **kw: FakeConn(cur))
    bootstrap._provision(_settings())
    joined = "\n".join(cur.executed)
    assert 'ALTER USER JETUSE_APP IDENTIFIED BY "App#Pw1"' in joined


def test_provision_survives_password_reuse_on_restart(patched):
    """FIX-58: コンテナ再起動時は同じパスワードでの ALTER USER が ORA-28007 になる。
    ここで失敗すると bootstrap 全体が諦めて migrate も RP 検証も走らないため、
    「すでに目的のパスワード」として続行できること。"""
    cur = FakeCursor(fail_create_user=True, fail_alter_password_reuse=True)
    patched.setattr(oracledb, "connect", lambda **kw: FakeConn(cur))
    bootstrap._provision(_settings())
    joined = "\n".join(cur.executed)
    assert "ENABLE_RESOURCE_PRINCIPAL" in joined  # 最後まで到達している
    assert bootstrap.resource_principal_status()["ok"] is True


def test_provision_raises_when_reused_password_cannot_log_in(patched):
    """ORA-28007 は「履歴にある」であって「現在のパスワード」とは限らないため、
    実ログインで裏取りし、入れなければ失敗として扱う(黙って進めない)。"""
    cur = FakeCursor(fail_create_user=True, fail_alter_password_reuse=True)

    def connect(**kw):
        if kw.get("user") == "JETUSE_APP":
            raise oracledb.DatabaseError(FakeErr(1017))  # invalid credential
        return FakeConn(cur)

    patched.setattr(oracledb, "connect", connect)
    with pytest.raises(oracledb.DatabaseError):
        bootstrap._provision(_settings())


def test_provision_skips_when_passwords_missing(patched, monkeypatch):
    monkeypatch.delenv("ADB_ADMIN_PASSWORD", raising=False)
    called = {"connect": False}
    patched.setattr(oracledb, "connect",
                    lambda **kw: called.__setitem__("connect", True) or FakeConn(FakeCursor()))
    bootstrap._provision(_settings())
    assert called["connect"] is False  # ADMINパスワード無しなら接続しない


def test_provision_success_reports_rp_status_ok(patched):
    # PORT-02: Select AI可視化(/api/health が読む resource_principal_status())
    cur = FakeCursor()
    patched.setattr(oracledb, "connect", lambda **kw: FakeConn(cur))
    bootstrap._provision(_settings())
    assert bootstrap.resource_principal_status() == {"ok": True}


def test_provision_rp_failure_reports_hint(patched):
    cur = FakeCursor(fail_rp=True)
    patched.setattr(oracledb, "connect", lambda **kw: FakeConn(cur))
    bootstrap._provision(_settings())
    status = bootstrap.resource_principal_status()
    assert status["ok"] is False
    assert "generative-ai-family" in status["hint"]


def test_bootstrap_runs_migrate(patched, monkeypatch):
    cur = FakeCursor()
    patched.setattr(oracledb, "connect", lambda **kw: FakeConn(cur))
    import jetuse_core.migrate as mig
    applied = {"v": False}
    monkeypatch.setattr(mig, "migrate", lambda: applied.__setitem__("v", True) or ["0001"])
    bootstrap.bootstrap()
    assert applied["v"] is True


def test_provision_marks_rp_not_ok_when_settings_missing(patched, monkeypatch):
    """FIX-58 F005: 設定不足で bootstrap を抜けると、起動時に書いた ok=None(実行中)が
    残り health が永久に「実行中」になる。理由つきで false を確定させる。"""
    monkeypatch.delenv("ADB_ADMIN_PASSWORD", raising=False)
    patched.setattr(oracledb, "connect", lambda **kw: FakeConn(FakeCursor()))
    bootstrap._set_resource_principal_status(None, "bootstrap 実行中")
    bootstrap._provision(_settings())
    status = bootstrap.resource_principal_status()
    assert status["ok"] is False
    assert "ADB_ADMIN_PASSWORD" in status["hint"]


def test_bootstrap_timeout_records_failure_in_status_file(patched, monkeypatch):
    """タイムアウトで諦めたときも「実行中」のままにしない(原因つきで false)。"""
    def boom(_settings):
        raise RuntimeError("DPY-4000: ADB unreachable")

    patched.setattr(bootstrap, "_provision", boom)
    monkeypatch.setattr(bootstrap, "BOOTSTRAP_TIMEOUT_S", 0)
    bootstrap.bootstrap()
    status = bootstrap.resource_principal_status()
    assert status["ok"] is False
    assert "タイムアウト" in status["hint"]
    assert "DPY-4000" in status["hint"]
