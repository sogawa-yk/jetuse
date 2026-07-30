"""ops/_adb.py と ops/setup-dev-schema.py の安全ゲートの単体テスト（RP-01 / ADR-0021）。

実環境 E2E は「正しい接続先で成功する」経路しか通らない。ここでは**通ってはいけない経路**
（接続先が同定できない / 権限が付いていない / 既存ユーザーのパスワードが不明）を、
OCI SDK と oracledb をスタブに置き換えて確認する。api パッケージのテストではないが、
`pytest packages/api/tests` が唯一の Python テスト入口なのでここに置く。
"""

import importlib.util
import json
import pathlib
import sys

import oracledb
import pytest

OPS = pathlib.Path(__file__).resolve().parents[3] / "ops"
if str(OPS) not in sys.path:
    sys.path.insert(0, str(OPS))

import _adb  # noqa: E402


def _load_setup_dev_schema():
    """ハイフン入りファイル名なので通常の import ができない。"""
    spec = importlib.util.spec_from_file_location("setup_dev_schema", OPS / "setup-dev-schema.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCursor:
    """execute された SQL を記録し、fetchone に既定値を返すだけのカーソル。"""

    def __init__(self, results=None):
        self.executed: list[tuple[str, dict]] = []
        self.results = list(results or [])

    def execute(self, sql, **kw):
        self.executed.append((sql, kw))

    def fetchone(self):
        return self.results.pop(0) if self.results else (0,)

    def sql_text(self) -> str:
        return " | ".join(sql for sql, _ in self.executed)


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


APPROVED = "ocid1.compartment.oc1..approved"     # 承認済みコンパートメント（ADB がいる想定）
CHILD = "ocid1.compartment.oc1..child"          # 承認済みの**子**（承認していない）
OTHER = "ocid1.compartment.oc1..otherdev"       # 同名階層を持つ別テナンシ側
TREE = {
    APPROVED: ("dev", "ocid1.compartment.oc1..jetuse"),
    CHILD: ("sandbox", APPROVED),
    OTHER: ("dev", "ocid1.compartment.oc1..otherjetuse"),
    "ocid1.compartment.oc1..otherjetuse": ("jetuse", "ocid1.tenancy.oc1..otherroot"),
    "ocid1.tenancy.oc1..otherroot": ("root", None),
}


def _stub_oci(monkeypatch, conn_strings: str, display_name: str = "adb-x",
              compartment: str = APPROVED):
    """`oci.database` / `oci.identity` を差し替える（ネットワークへ出さない）。"""
    import oci

    class Strings:
        all_connection_strings = {"low": conn_strings}
        profiles = []
        high = medium = low = dedicated = None

    class Adb:
        connection_strings = Strings()
        compartment_id = compartment

    Adb.display_name = display_name

    class DbClient:
        def __init__(self, **kw):
            pass

        def get_autonomous_database(self, _ocid):
            return type("R", (), {"data": Adb()})()

    class IdClient:
        def __init__(self, **kw):
            pass

        def get_compartment(self, ocid):
            name, parent = TREE[ocid]
            return type("R", (), {"data": type("C", (), {"name": name,
                                                         "compartment_id": parent})()})()

    monkeypatch.setenv("COMPARTMENT_OCID", APPROVED)
    monkeypatch.delenv("ADB_COMPARTMENT_OCID", raising=False)
    monkeypatch.setattr(_adb, "_dotenv", {})
    monkeypatch.setattr(oci.database, "DatabaseClient", DbClient)
    monkeypatch.setattr(oci.identity, "IdentityClient", IdClient)
    monkeypatch.setattr("jetuse_core.oci_auth.sdk_signer_args", lambda *_a, **_k: {"config": {}})


# ---------------------------------------------------------------- assert_schema
@pytest.mark.parametrize("bad", ["", "1abc", "a b", "JETUSE-APP", "x" * 31, "JETUSE;DROP"])
def test_assert_schema_rejects_injection_and_malformed(bad):
    with pytest.raises(SystemExit):
        _adb.assert_schema(bad)


def test_assert_schema_normalizes_case():
    assert _adb.assert_schema("jetuse_alice") == "JETUSE_ALICE"


# ---------------------------------------------------------------- assert_target
def test_assert_target_without_adb_ocid_aborts(monkeypatch):
    """ADB_OCID が無ければ接続先を同定できない＝DDL を実行させない。"""
    monkeypatch.setenv("ADB_OCID", "")
    monkeypatch.setattr(_adb, "_dotenv", {})
    cur = FakeCursor([("TOKEN_JETUSELOOP2",)])
    with pytest.raises(SystemExit):
        _adb.assert_target(FakeConn(cur))


def test_assert_target_aborts_when_sql_target_is_another_adb(monkeypatch):
    """DB_NAME のインスタンス固有トークンが ADB の接続文字列に無ければ別 ADB。"""
    monkeypatch.setenv("ADB_OCID", "ocid1.autonomousdatabase.oc1..x")
    monkeypatch.setenv("OCI_REGION", "ap-osaka-1")
    _stub_oci(monkeypatch, conn_strings="(description=(host=aaaa1111_otherdb_low.adb...))")
    cur = FakeCursor([("G912A29DFC5DE89_JETUSELOOP2",)])
    with pytest.raises(SystemExit) as ei:
        _adb.assert_target(FakeConn(cur))
    assert "同一だと" in str(ei.value)


def test_assert_target_passes_when_token_matches(monkeypatch, capsys):
    monkeypatch.setenv("ADB_OCID", "ocid1.autonomousdatabase.oc1..x")
    monkeypatch.setenv("OCI_REGION", "ap-osaka-1")
    _stub_oci(monkeypatch,
              conn_strings="(description=(host=g912a29dfc5de89_jetuseloop2_low.adb...))")
    cur = FakeCursor([("G912A29DFC5DE89_JETUSELOOP2",)])
    assert _adb.assert_target(FakeConn(cur)) == "G912A29DFC5DE89_JETUSELOOP2"


@pytest.mark.parametrize("compartment,why", [
    (OTHER, "同名の dev 階層を持つ別テナンシ"),
    (CHILD, "承認済みコンパートメントの子（承認していない兄弟・子孫）"),
])
def test_assert_target_aborts_outside_approved_compartment(monkeypatch, compartment, why):
    """ADB_OCID・ウォレット・プロファイルが揃って別環境を指していても止める。

    認可は**完全一致**。名前が同じ別テナンシも、承認済みの配下（子）も通さない。
    """
    monkeypatch.setenv("ADB_OCID", "ocid1.autonomousdatabase.oc1..x")
    _stub_oci(monkeypatch,
              conn_strings="(description=(host=g912a29dfc5de89_jetuseloop2_low.adb...))",
              compartment=compartment)
    cur = FakeCursor([("G912A29DFC5DE89_JETUSELOOP2",)])
    with pytest.raises(SystemExit) as ei:
        _adb.assert_target(FakeConn(cur))
    assert "承認済みコンパートメント" in str(ei.value), why


def test_adb_compartment_ocid_takes_precedence(monkeypatch):
    """ADB が子コンパートメントにある環境は ADB_COMPARTMENT_OCID で明示する。"""
    monkeypatch.setenv("ADB_OCID", "ocid1.autonomousdatabase.oc1..x")
    _stub_oci(monkeypatch,
              conn_strings="(description=(host=g912a29dfc5de89_jetuseloop2_low.adb...))",
              compartment=CHILD)
    monkeypatch.setenv("ADB_COMPARTMENT_OCID", CHILD)
    cur = FakeCursor([("G912A29DFC5DE89_JETUSELOOP2",)])
    assert _adb.assert_target(FakeConn(cur)) == "G912A29DFC5DE89_JETUSELOOP2"


def test_adb_compartment_ocid_override_must_be_under_approved_root(monkeypatch):
    """上書きが別テナンシへの抜け穴にならない（承認済みの根の配下であることを遡って確認）。"""
    monkeypatch.setenv("ADB_OCID", "ocid1.autonomousdatabase.oc1..x")
    _stub_oci(monkeypatch,
              conn_strings="(description=(host=g912a29dfc5de89_jetuseloop2_low.adb...))",
              compartment=OTHER)
    monkeypatch.setenv("ADB_COMPARTMENT_OCID", OTHER)
    cur = FakeCursor([("G912A29DFC5DE89_JETUSELOOP2",)])
    with pytest.raises(SystemExit) as ei:
        _adb.assert_target(FakeConn(cur))
    assert "承認済みの根" in str(ei.value)


def test_assert_target_aborts_without_approved_compartment_env(monkeypatch):
    monkeypatch.setenv("ADB_OCID", "ocid1.autonomousdatabase.oc1..x")
    _stub_oci(monkeypatch,
              conn_strings="(description=(host=g912a29dfc5de89_jetuseloop2_low.adb...))")
    monkeypatch.setenv("COMPARTMENT_OCID", "")
    monkeypatch.setattr(_adb, "_dotenv", {})
    cur = FakeCursor([("G912A29DFC5DE89_JETUSELOOP2",)])
    with pytest.raises(SystemExit):
        _adb.assert_target(FakeConn(cur))


# ---------------------------------------------------------------- call_timeout
@pytest.mark.parametrize("bad", ["0", "-1", "abc"])
def test_call_timeout_rejects_non_positive(monkeypatch, bad):
    """接続後の SQL 往復に無期限待ちを許さない（セットアップが黙って止まる）。"""
    monkeypatch.setenv("ADB_CALL_TIMEOUT_MS", bad)
    with pytest.raises(SystemExit):
        _adb.call_timeout_ms()


def test_call_timeout_default_and_override(monkeypatch):
    monkeypatch.delenv("ADB_CALL_TIMEOUT_MS", raising=False)
    monkeypatch.setattr(_adb, "_dotenv", {})
    assert _adb.call_timeout_ms() == _adb.DEFAULT_CALL_TIMEOUT_MS
    monkeypatch.setenv("ADB_CALL_TIMEOUT_MS", "30000")
    assert _adb.call_timeout_ms() == 30000


def test_assert_target_aborts_when_db_name_has_no_token(monkeypatch):
    """`<token>_<db_name>` 形式でなければ同一性を証明できない＝中止。"""
    monkeypatch.setenv("ADB_OCID", "ocid1.autonomousdatabase.oc1..x")
    _stub_oci(monkeypatch, conn_strings="(description=(host=anything))")
    cur = FakeCursor([("JETUSELOOP2",)])
    with pytest.raises(SystemExit):
        _adb.assert_target(FakeConn(cur))


# ------------------------------------------------- enable_resource_principal
def test_enable_resource_principal_aborts_when_grant_missing():
    """呼び出しが成功しても EXECUTE が付いていなければ中止する（ORA-20404 の予防）。"""
    cur = FakeCursor([(0,)])
    with pytest.raises(SystemExit) as ei:
        _adb.enable_resource_principal(cur, "JETUSE_ALICE")
    assert "ORA-20404" in str(ei.value)
    assert "ENABLE_RESOURCE_PRINCIPAL" in cur.sql_text()


def test_enable_resource_principal_ok_when_granted(capsys):
    cur = FakeCursor([(1,)])
    _adb.enable_resource_principal(cur, "JETUSE_ALICE")
    assert "OCI$RESOURCE_PRINCIPAL" in capsys.readouterr().out


def test_rp_granted_requires_admin_owned_credential():
    """別スキーマの同名オブジェクトへの grant を「付与済み」と誤認しない。"""
    cur = FakeCursor([(0,)])
    _adb.rp_granted(cur, "JETUSE_ALICE")
    sql, binds = cur.executed[0]
    assert "owner = 'ADMIN'" in sql
    assert binds == {"u": "JETUSE_ALICE", "c": "OCI$RESOURCE_PRINCIPAL"}


# ---------------------------------------------------------------- assert_password
@pytest.mark.parametrize("bad", ['Pw"1', 'Pw 1', "Pw\n1", "Pw\t1", "", "x" * 61])
def test_assert_password_rejects_quote_and_whitespace(bad):
    """`IDENTIFIED BY "..."` に埋めるため、引用符・空白・改行は受け付けない。"""
    with pytest.raises(SystemExit):
        _adb.assert_password("JETUSE_ALICE", bad)


def test_assert_password_accepts_generated_form():
    assert _adb.assert_password("JETUSE_ALICE", "GxAbc123def456#7") == "GxAbc123def456#7"


def test_ensure_user_rejects_password_before_any_ddl():
    """壊れたパスワードでは DDL を 1 本も出さない。"""
    mod = _load_setup_dev_schema()
    cur = FakeCursor([(0,)])
    with pytest.raises(SystemExit):
        mod._ensure_user(cur, "JETUSE_ALICE", 'bad"pw', explicit=True)
    assert cur.executed == []


# ---------------------------------------------------------------- _ensure_user
def _ora(code: int) -> oracledb.DatabaseError:
    return oracledb.DatabaseError(type("E", (), {"code": code, "message": f"ORA-{code:05d}"})())


def test_ensure_user_creates_when_absent():
    mod = _load_setup_dev_schema()
    cur = ReceiptCursor(exists=0)
    assert mod._ensure_user(cur, "JETUSE_ALICE", "Pw#1", explicit=False) is True
    assert "CREATE USER" in cur.sql_text()


def test_ensure_user_keeps_password_of_existing_user_when_not_explicit():
    """無指定の再実行で既存ユーザーのパスワードを勝手に変えない（配備済みスタックを壊さない）。"""
    mod = _load_setup_dev_schema()
    cur = FakeCursor([(1,)])
    assert mod._ensure_user(cur, "JETUSE_ALICE", "Pw#1", explicit=False) is False
    assert "ALTER USER" not in cur.sql_text()


def test_ensure_user_ora28007_requires_real_login(monkeypatch):
    """ORA-28007 は「履歴にある」であって現在値とは限らない → 実ログインで裏取りする。"""
    mod = _load_setup_dev_schema()

    class Cur(FakeCursor):
        def execute(self, sql, **kw):
            super().execute(sql, **kw)
            if sql.startswith("ALTER USER"):
                raise _ora(28007)

    monkeypatch.setattr(_adb, "verify_login", lambda *_: True)
    cur = Cur([(1,)])
    assert mod._ensure_user(cur, "JETUSE_ALICE", "Pw#1", explicit=True) is True

    monkeypatch.setattr(_adb, "verify_login", lambda *_: False)
    cur = Cur([(1,)])
    with pytest.raises(SystemExit):
        mod._ensure_user(cur, "JETUSE_ALICE", "Pw#1", explicit=True)


def test_select_ai_credential_default_is_resource_principal(monkeypatch):
    """既定は RP（ADR-0021）。旧 API キー名は env で上書きできる（後方互換）。"""
    from jetuse_core.settings import Settings, get_settings

    monkeypatch.delenv("SELECT_AI_CREDENTIAL", raising=False)
    get_settings.cache_clear()
    assert Settings(_env_file=None).select_ai_credential == "OCI$RESOURCE_PRINCIPAL"
    monkeypatch.setenv("SELECT_AI_CREDENTIAL", "JETUSE_OCI_CRED")
    get_settings.cache_clear()
    assert get_settings().select_ai_credential == "JETUSE_OCI_CRED"
    get_settings.cache_clear()


def test_ensure_user_require_new_aborts_on_existing_user():
    """新規作成のはずの呼び出しで既存が見つかったら、ALTER せずに中止する（競合対策）。"""
    mod = _load_setup_dev_schema()
    cur = FakeCursor([(1,)])
    with pytest.raises(SystemExit) as ei:
        mod._ensure_user(cur, "JETUSE_ALICE", "Pw#1", explicit=True, require_new=True)
    assert "--require-new" in str(ei.value)
    assert "ALTER USER" not in cur.sql_text() and "CREATE USER" not in cur.sql_text()


def test_ensure_user_require_new_creates_when_absent():
    mod = _load_setup_dev_schema()
    cur = ReceiptCursor(exists=0)
    assert mod._ensure_user(cur, "JETUSE_ALICE", "Pw#1", explicit=True, require_new=True) is True
    assert "CREATE USER" in cur.sql_text()


# ---------------------------------------------------------------- receipt（作成の証跡）
class FakeVar:
    def __init__(self, value=None):
        self._v = value

    def getvalue(self):
        return self._v


class ReceiptCursor(FakeCursor):
    """`dba_users` 照会に user_id / created を返し、DDL は記録するだけのカーソル。

    `CREATE USER` は PL/SQL ブロック 1 本（`_create_user_with_receipt`）なので、
    その OUT バインドに値を詰めて返す。
    """

    def __init__(self, exists: int, user_id: int = 4242, fail_after_create: bool = False):
        super().__init__()
        self.exists, self.uid, self.fail_after_create = exists, user_id, fail_after_create

    def var(self, typ):
        return FakeVar(self.uid if typ is int else "2026-07-29 12:00:00")

    def execute(self, sql, **kw):
        super().execute(sql, **kw)
        if self.fail_after_create and sql.startswith("GRANT"):
            raise _ora(1031)  # ORA-01031: insufficient privileges（CREATE 後の失敗を注入）

    def fetchone(self):
        sql = self.executed[-1][0]
        if "COUNT(*)" in sql:
            return (self.exists,)
        if "user_id" in sql:
            return (self.uid,)
        if "TO_CHAR(created" in sql:
            return ("2026-07-29 12:00:00",)
        return (0,)


def test_receipt_written_immediately_after_create(tmp_path):
    """CREATE の直後に receipt が出る＝後続が失敗しても『自分が作った』を確定できる。"""
    mod = _load_setup_dev_schema()
    rec = tmp_path / "receipt.json"
    cur = ReceiptCursor(exists=0)
    assert mod._ensure_user(cur, "JETUSE_ALICE", "Pw#1", explicit=True, receipt=str(rec)) is True
    entries = json.loads(rec.read_text())
    assert entries == [{"user": "JETUSE_ALICE", "user_id": 4242,
                        "created_at": "2026-07-29 12:00:00", "created_by_this_run": True}]
    # CREATE と識別子取得が 1 本の PL/SQL（＝クライアント側に読み直しの窓が無い）
    sql = cur.sql_text()
    assert "CREATE USER" in sql and "INTO :id, :cr" in sql


def test_receipt_survives_failure_after_create(tmp_path):
    """CREATE 後の GRANT 失敗（＝setup 全体は失敗）でも receipt は残る。"""
    mod = _load_setup_dev_schema()
    rec = tmp_path / "receipt.json"
    cur = ReceiptCursor(exists=0, fail_after_create=True)
    mod._ensure_user(cur, "JETUSE_ALICE", "Pw#1", explicit=True, receipt=str(rec))
    with pytest.raises(oracledb.DatabaseError):
        cur.execute("GRANT CREATE SESSION TO JETUSE_ALICE")
    assert json.loads(rec.read_text())[0]["created_by_this_run"] is True


def test_receipt_marks_existing_user_as_not_ours(tmp_path):
    """実行前から在ったユーザーは `created_by_this_run=false`＝呼び出し側が所有物にしない。"""
    mod = _load_setup_dev_schema()
    rec = tmp_path / "receipt.json"
    cur = ReceiptCursor(exists=1)
    mod._ensure_user(cur, "JETUSE_ALICE", "Pw#1", explicit=False, receipt=str(rec))
    assert json.loads(rec.read_text())[0]["created_by_this_run"] is False


@pytest.mark.parametrize("broken", ["not json at all", '{"user": "x"}'])
def test_receipt_path_validated_before_ddl(tmp_path, broken):
    """出力先が壊れている/書けないなら、**DDL を 1 本も出さずに**中止する。"""
    mod = _load_setup_dev_schema()
    rec = tmp_path / "receipt.json"
    rec.write_text(broken)
    with pytest.raises(SystemExit):
        mod._assert_receipt_writable(str(rec))
    with pytest.raises(SystemExit):
        mod._assert_receipt_writable(str(tmp_path / "no" / "such" / "dir" / "receipt.json"))


def test_receipt_path_ok_when_absent_or_valid(tmp_path):
    mod = _load_setup_dev_schema()
    mod._assert_receipt_writable(str(tmp_path / "new.json"))      # 未作成でよい
    (tmp_path / "ok.json").write_text("[]")
    mod._assert_receipt_writable(str(tmp_path / "ok.json"))
    mod._assert_receipt_writable("")                              # 未指定は何もしない


def test_receipt_is_optional():
    """`--receipt` 無しでも従来どおり動く（通常の開発者操作）。"""
    mod = _load_setup_dev_schema()
    cur = ReceiptCursor(exists=0)
    assert mod._ensure_user(cur, "JETUSE_ALICE", "Pw#1", explicit=True) is True


def test_ensure_user_reraises_other_oracle_errors():
    mod = _load_setup_dev_schema()

    class Cur(FakeCursor):
        def execute(self, sql, **kw):
            super().execute(sql, **kw)
            if sql.startswith("ALTER USER"):
                raise _ora(1918)  # ORA-01918: user does not exist

    with pytest.raises(oracledb.DatabaseError):
        mod._ensure_user(Cur([(1,)]), "JETUSE_ALICE", "Pw#1", explicit=True)
