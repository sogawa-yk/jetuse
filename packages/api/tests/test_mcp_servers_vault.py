"""BE-08: 認証付き MCP 登録の Vault 書込経路の単体テスト。

OCI クライアントと DB をモックし、_write_secret / create_server / delete_server の
正常系・権限エラー・一時障害・ACTIVE 待ち・補償削除・provenance を検証する(BE08-007)。
実 Vault への書込は IAM 人間ゲートのため、ここでは到達しない(SKIPPED.md 参照)。
"""

import base64
import contextlib
import datetime

import oci
import pytest

import jetuse_core.mcp_servers as mcp
from jetuse_core.mcp_servers import VaultWriteError
from jetuse_core.settings import Settings

# --- フェイク OCI VaultsClient ---

class _Resp:
    def __init__(self, data):
        self.data = data


class _Secret:
    def __init__(self, sid, state="ACTIVE"):
        self.id = sid
        self.lifecycle_state = state


class FakeVaults:
    def __init__(self, create_error=None, states=None, delete_error=None):
        self.create_error = create_error
        self.delete_error = delete_error
        self.states = states or ["ACTIVE"]  # get_secret が返す状態の列(順に消費)
        self.created = []     # (details, kwargs)
        self.deleted = []     # (secret_id, details)
        self._id = "ocid1.vaultsecret.oc1..fake0001"
        self._poll = 0

    def create_secret(self, details, **kw):
        if self.create_error:
            raise self.create_error
        self.created.append((details, kw))
        return _Resp(_Secret(self._id))

    def get_secret(self, sid):
        i = min(self._poll, len(self.states) - 1)
        self._poll += 1
        return _Resp(_Secret(sid, self.states[i]))

    def schedule_secret_deletion(self, sid, details):
        if self.delete_error:
            raise self.delete_error
        self.deleted.append((sid, details))
        return _Resp(None)


# --- フェイク DB ---

class FakeCursor:
    def __init__(self, sink, fail_on_insert=False, select_row=None):
        self.sink = sink
        self.fail_on_insert = fail_on_insert
        self.select_row = select_row
        self.rowcount = 0
        self._last = None
        self.executed = []  # 実行された文種(INSERT/SELECT/DELETE)を記録

    def execute(self, sql, **binds):
        s = sql.strip()
        self.executed.append(s.split()[0])
        if s.startswith("INSERT"):
            if self.fail_on_insert:
                raise RuntimeError("DB insert boom")
            self.sink["insert"] = binds
            self.rowcount = 1
        elif s.startswith("SELECT"):
            self._last = self.select_row
        elif s.startswith("DELETE"):
            self.rowcount = 1 if self.select_row else 0

    def fetchone(self):
        return self._last


class FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.committed = False

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed = True


def _fake_connect(cur):
    @contextlib.contextmanager
    def _cm():
        yield FakeConn(cur)
    return _cm


def _configured():
    return Settings(
        vault_ocid="ocid1.vault.oc1..v",
        vault_key_ocid="ocid1.key.oc1..k",
        compartment_ocid="ocid1.compartment.oc1..c",
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # _write_secret は関数内で `import time` するため実 time モジュールの sleep を潰せばよい。
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda *a, **k: None)


def test_create_server_writes_to_vault_and_stores_ocid_only(monkeypatch):
    sink = {}
    fake = FakeVaults()
    monkeypatch.setattr(mcp, "get_settings", _configured)
    monkeypatch.setattr(mcp, "_vault_client", lambda: fake)
    monkeypatch.setattr(mcp, "connect", _fake_connect(FakeCursor(sink)))

    res = mcp.create_server("user-1", "lbl", "https://example.com/mcp", auth_token="tok-XYZ")

    assert res["has_auth"] is True
    # Vault に 1 件作成・内容は base64(token)・retry_token あり・名前は mcp-<id>
    assert len(fake.created) == 1
    details, kwargs = fake.created[0]
    assert base64.b64decode(details.secret_content.content).decode() == "tok-XYZ"
    assert details.secret_name.startswith("mcp-")
    assert kwargs.get("opc_retry_token")
    # DB には OCID 参照のみ・managed=1・平文トークンは一切入らない
    binds = sink["insert"]
    assert binds["a"] == fake._id
    assert binds["m"] == 1
    assert "tok-XYZ" not in "".join(str(v) for v in binds.values())


def test_create_server_no_auth_skips_vault(monkeypatch):
    sink = {}
    fake = FakeVaults()
    monkeypatch.setattr(mcp, "get_settings", _configured)
    monkeypatch.setattr(mcp, "_vault_client", lambda: fake)
    monkeypatch.setattr(mcp, "connect", _fake_connect(FakeCursor(sink)))

    res = mcp.create_server("user-1", "lbl", "https://example.com/mcp")
    assert res["has_auth"] is False
    assert fake.created == []
    assert sink["insert"]["a"] is None
    assert sink["insert"]["m"] == 0


def test_create_server_external_ocid_backcompat(monkeypatch):
    # BE08-R3-004: 第4位置引数=外部作成済み OCID を従来通り保存(secret_managed=0・Vault 不触)。
    sink = {}
    fake = FakeVaults()
    monkeypatch.setattr(mcp, "get_settings", _configured)
    monkeypatch.setattr(mcp, "_vault_client", lambda: fake)
    monkeypatch.setattr(mcp, "connect", _fake_connect(FakeCursor(sink)))

    ext = "ocid1.vaultsecret.oc1..external-managed"
    res = mcp.create_server("user-1", "lbl", "https://example.com/mcp", ext)
    assert res["has_auth"] is True
    assert fake.created == []  # 新規 secret は作らない
    assert sink["insert"]["a"] == ext
    assert sink["insert"]["m"] == 0


def test_create_server_rejects_both_auth_inputs(monkeypatch):
    monkeypatch.setattr(mcp, "get_settings", _configured)
    monkeypatch.setattr(mcp, "_vault_client", lambda: FakeVaults())
    monkeypatch.setattr(mcp, "connect", _fake_connect(FakeCursor({})))
    with pytest.raises(ValueError):
        mcp.create_server("u", "l", "https://example.com/mcp", "ocid1...", auth_token="tok")


def test_create_server_empty_token_is_no_auth(monkeypatch):
    # BE08-R3-005: 空文字/空白のみは認証なしに正規化(本番とテストダブルの判定一致)。
    sink = {}
    fake = FakeVaults()
    monkeypatch.setattr(mcp, "get_settings", _configured)
    monkeypatch.setattr(mcp, "_vault_client", lambda: fake)
    monkeypatch.setattr(mcp, "connect", _fake_connect(FakeCursor(sink)))

    res = mcp.create_server("u", "l", "https://example.com/mcp", auth_token="   ")
    assert res["has_auth"] is False
    assert fake.created == []
    assert sink["insert"]["m"] == 0


@pytest.mark.parametrize(
    "bad", ["bad\ntoken", "abc\n", "\nabc", "abc def", " abc", "abc ", "tok\tx", "トークン"]
)
def test_create_server_rejects_unsafe_token(monkeypatch, bad):
    # BE08-R3-005/R4-004: 内部空白・先頭末尾空白・制御文字・非ASCII は拒否(silent 変更しない)。
    monkeypatch.setattr(mcp, "get_settings", _configured)
    monkeypatch.setattr(mcp, "_vault_client", lambda: FakeVaults())
    monkeypatch.setattr(mcp, "connect", _fake_connect(FakeCursor({})))
    with pytest.raises(ValueError):
        mcp.create_server("u", "l", "https://example.com/mcp", auth_token=bad)


def test_create_server_db_failure_compensates_secret(monkeypatch):
    fake = FakeVaults()
    monkeypatch.setattr(mcp, "get_settings", _configured)
    monkeypatch.setattr(mcp, "_vault_client", lambda: fake)
    monkeypatch.setattr(mcp, "connect", _fake_connect(FakeCursor({}, fail_on_insert=True)))

    with pytest.raises(RuntimeError):
        mcp.create_server("user-1", "lbl", "https://example.com/mcp", auth_token="tok")
    # 孤児防止: 作成済み secret を削除予約している
    assert [d[0] for d in fake.deleted] == [fake._id]


def test_write_secret_permission_error_failclosed(monkeypatch):
    err = oci.exceptions.ServiceError(403, "NotAuthorized", {}, "denied")
    fake = FakeVaults(create_error=err)
    monkeypatch.setattr(mcp, "get_settings", _configured)
    monkeypatch.setattr(mcp, "_vault_client", lambda: fake)

    with pytest.raises(VaultWriteError):
        mcp._write_secret("mcp-x", "tok")


def test_write_secret_throttling_maps_to_vaultwriteerror(monkeypatch):
    err = oci.exceptions.ServiceError(429, "TooManyRequests", {}, "slow down")
    fake = FakeVaults(create_error=err)
    monkeypatch.setattr(mcp, "get_settings", _configured)
    monkeypatch.setattr(mcp, "_vault_client", lambda: fake)

    with pytest.raises(VaultWriteError):
        mcp._write_secret("mcp-x", "tok")


@pytest.mark.parametrize(
    ("status", "marker"),
    [(403, "人間ゲート"), (429, "一時障害"), (500, "一時障害"), (400, "恒久的")],
)
def test_vault_service_error_classification(status, marker):
    # すべて VaultWriteError(→503)だが、恒久/一時/権限をメッセージで区別する(BE08-005)。
    err = oci.exceptions.ServiceError(status, "X", {}, "msg")
    wrapped = mcp._vault_service_error(err)
    assert isinstance(wrapped, VaultWriteError)
    assert marker in str(wrapped)


def test_write_secret_failed_state_compensates(monkeypatch):
    fake = FakeVaults(states=["CREATING", "FAILED"])
    monkeypatch.setattr(mcp, "get_settings", _configured)
    monkeypatch.setattr(mcp, "_vault_client", lambda: fake)

    with pytest.raises(VaultWriteError):
        mcp._write_secret("mcp-x", "tok")
    assert [d[0] for d in fake.deleted] == [fake._id]


def test_write_secret_waits_for_active(monkeypatch):
    fake = FakeVaults(states=["CREATING", "CREATING", "ACTIVE"])
    monkeypatch.setattr(mcp, "get_settings", _configured)
    monkeypatch.setattr(mcp, "_vault_client", lambda: fake)

    sid = mcp._write_secret("mcp-x", "tok")
    assert sid == fake._id
    assert fake.deleted == []  # ACTIVE まで待てたので補償なし


def test_write_secret_unconfigured_failclosed(monkeypatch):
    monkeypatch.setattr(mcp, "get_settings", lambda: Settings())
    with pytest.raises(VaultWriteError):
        mcp._write_secret("mcp-x", "tok")


def test_delete_server_schedules_managed_secret_deletion(monkeypatch):
    fake = FakeVaults()
    monkeypatch.setattr(mcp, "get_settings", _configured)
    monkeypatch.setattr(mcp, "_vault_client", lambda: fake)
    # 管理 secret 行(secret_managed=1)
    cur = FakeCursor({}, select_row=("ocid1.vaultsecret.oc1..stored", 1))
    monkeypatch.setattr(mcp, "connect", _fake_connect(cur))

    assert mcp.delete_server("user-1", "sid-1") is True
    assert [d[0] for d in fake.deleted] == ["ocid1.vaultsecret.oc1..stored"]
    # 削除予約が DELETE より前に走る(順序の証跡 / BE08-R2-002)
    assert cur.executed == ["SELECT", "DELETE"]


def test_delete_server_keeps_row_when_secret_deletion_fails(monkeypatch):
    # BE08-R2-002: 削除予約に失敗したら DB 行を残し VaultWriteError(→503)。孤児を作らない。
    err = oci.exceptions.ServiceError(429, "TooManyRequests", {}, "slow down")
    fake = FakeVaults(delete_error=err)
    monkeypatch.setattr(mcp, "get_settings", _configured)
    monkeypatch.setattr(mcp, "_vault_client", lambda: fake)
    cur = FakeCursor({}, select_row=("ocid1.vaultsecret.oc1..stored", 1))
    monkeypatch.setattr(mcp, "connect", _fake_connect(cur))

    with pytest.raises(VaultWriteError):
        mcp.delete_server("user-1", "sid-1")
    assert "DELETE" not in cur.executed  # 行は残る(再試行可能)


def test_delete_server_idempotent_when_409_and_confirmed_deleting(monkeypatch):
    # BE08-R3-002/R4-001: 409 かつ get_secret で削除進行中を確認できたときだけ冪等成功。
    err = oci.exceptions.ServiceError(409, "Conflict", {}, "already")
    fake = FakeVaults(delete_error=err, states=["PENDING_DELETION"])
    monkeypatch.setattr(mcp, "get_settings", _configured)
    monkeypatch.setattr(mcp, "_vault_client", lambda: fake)
    cur = FakeCursor({}, select_row=("ocid1.vaultsecret.oc1..stored", 1))
    monkeypatch.setattr(mcp, "connect", _fake_connect(cur))

    assert mcp.delete_server("user-1", "sid-1") is True
    assert "DELETE" in cur.executed  # 行は削除される(詰まらない)


def test_delete_server_409_without_deleting_state_fail_closed(monkeypatch):
    # BE08-R4-001: 409 でも削除進行中を確認できない(=単なる状態競合)なら fail-closed。
    err = oci.exceptions.ServiceError(409, "Conflict", {}, "conflict")
    fake = FakeVaults(delete_error=err, states=["ACTIVE"])
    monkeypatch.setattr(mcp, "get_settings", _configured)
    monkeypatch.setattr(mcp, "_vault_client", lambda: fake)
    cur = FakeCursor({}, select_row=("ocid1.vaultsecret.oc1..stored", 1))
    monkeypatch.setattr(mcp, "connect", _fake_connect(cur))

    with pytest.raises(VaultWriteError):
        mcp.delete_server("user-1", "sid-1")
    assert "DELETE" not in cur.executed  # 行は残る


def test_delete_server_409_cancelling_deletion_fail_closed(monkeypatch):
    # BE08-R5-002: CANCELLING_DELETION は ACTIVE へ戻る途中 → 削除進行中扱いせず fail-closed。
    err = oci.exceptions.ServiceError(409, "Conflict", {}, "cancelling")
    fake = FakeVaults(delete_error=err, states=["CANCELLING_DELETION"])
    monkeypatch.setattr(mcp, "get_settings", _configured)
    monkeypatch.setattr(mcp, "_vault_client", lambda: fake)
    cur = FakeCursor({}, select_row=("ocid1.vaultsecret.oc1..stored", 1))
    monkeypatch.setattr(mcp, "connect", _fake_connect(cur))

    with pytest.raises(VaultWriteError):
        mcp.delete_server("user-1", "sid-1")
    assert "DELETE" not in cur.executed


def test_delete_server_404_fail_closed(monkeypatch):
    # BE08-R4-001: 404(NotAuthorizedOrNotFound は権限不足と不在を区別しない)は fail-closed。
    err = oci.exceptions.ServiceError(404, "NotAuthorizedOrNotFound", {}, "nf")
    fake = FakeVaults(delete_error=err)
    monkeypatch.setattr(mcp, "get_settings", _configured)
    monkeypatch.setattr(mcp, "_vault_client", lambda: fake)
    cur = FakeCursor({}, select_row=("ocid1.vaultsecret.oc1..stored", 1))
    monkeypatch.setattr(mcp, "connect", _fake_connect(cur))

    with pytest.raises(VaultWriteError):
        mcp.delete_server("user-1", "sid-1")
    assert "DELETE" not in cur.executed  # 行は残る(live secret を残さない)


def test_schedule_deletion_time_above_oci_lower_bound(monkeypatch):
    # BE08-R3-001: time_of_deletion は「受信時点から1日」の下限を割らない(余裕を持つ)。
    captured = {}

    class _V:
        def schedule_secret_deletion(self, sid, details):
            captured["when"] = details.time_of_deletion

    monkeypatch.setattr(mcp, "_vault_client", lambda: _V())
    mcp._schedule_secret_deletion("ocid1.vaultsecret.oc1..x")
    now = datetime.datetime.now(datetime.UTC)
    # 1日(下限)＋通信遅延の余裕を確保。少なくとも 1 日超、30 日以内。
    assert captured["when"] > now + datetime.timedelta(days=1)
    assert captured["when"] < now + datetime.timedelta(days=30)


def test_delete_server_skips_external_unmanaged_secret(monkeypatch):
    fake = FakeVaults()
    monkeypatch.setattr(mcp, "get_settings", _configured)
    monkeypatch.setattr(mcp, "_vault_client", lambda: fake)
    # 外部管理 OCID(secret_managed=0)は触らない
    cur = FakeCursor({}, select_row=("ocid1.vaultsecret.oc1..external", 0))
    monkeypatch.setattr(mcp, "connect", _fake_connect(cur))

    assert mcp.delete_server("user-1", "sid-1") is True
    assert fake.deleted == []


def test_delete_server_missing_row_returns_false(monkeypatch):
    cur = FakeCursor({}, select_row=None)
    monkeypatch.setattr(mcp, "connect", _fake_connect(cur))
    assert mcp.delete_server("user-1", "nope") is False
