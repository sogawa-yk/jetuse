"""インストール記録リポジトリ(PLG-02)と migration の単体テスト。

実 ADB には接続せず、`installed_plugins` を再現するインメモリの fake 接続で store の
CRUD を往復させる。migration は (1) SQL ファイルが期待 DDL を含むこと、(2) ランナーが
再適用で冪等(2 回目は何も適用せず例外も出ない)であることを fake プールで検証する。
実機(ローカル ADB)での適用確認は人間ゲートの最終テストに委ねる。
"""

import contextlib
import json
import pathlib
import re

import pytest

import jetuse_core.migrate as mig
from jetuse_core.plugins import store
from jetuse_core.plugins.manifest import SCHEMA_VERSION, validate_manifest

MIGRATIONS_DIR = pathlib.Path(mig.__file__).parent / "migrations"


def _manifest(plugin_id="acme/faq-summarizer", version="1.2.0"):
    return validate_manifest(
        {
            "schemaVersion": SCHEMA_VERSION,
            "id": plugin_id,
            "version": version,
            "kind": "usecase",
            "name": "FAQ要約",
            "publisher": "acme-corp",
            "jetuse": {"minVersion": "0.3.0"},
            "permissions": ["platform:rag.search"],
            "contributes": {
                "usecase": {
                    "fields": [{"name": "text", "type": "textarea"}],
                    "template": "要約して: {{text}}",
                }
            },
        }
    )


# --- store を支える fake 接続 -------------------------------------------------

# _COLS の並びに対応するタプルを返す(store._row_to_record が読む順序)。
_COL_ORDER = [
    "id",
    "plugin_id",
    "version",
    "kind",
    "source_registry",
    "manifest",
    "signature_verified",
    "installed_by",
    "installed_at",
]


class _UniqueViolation(Exception):
    """(plugin_id, version) 一意制約違反を模す(Oracle の ORA-00001 相当)。"""


class FakeStoreDB:
    """installed_plugins をインメモリで再現する。connect() ごとに同じ表を共有する。"""

    def __init__(self):
        self.rows: list[dict] = []
        self._seq = 0

    def _row_tuple(self, row: dict):
        return tuple(row[c] for c in _COL_ORDER)


class FakeStoreCursor:
    def __init__(self, db: FakeStoreDB):
        self.db = db
        self.rowcount = 0
        self._result: list[tuple] = []

    def execute(self, sql: str, **binds):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO installed_plugins"):
            if any(
                r["plugin_id"] == binds["pid"] and r["version"] == binds["ver"]
                for r in self.db.rows
            ):
                raise _UniqueViolation("uq_installed_plugin_ver")
            self.db._seq += 1
            self.db.rows.append(
                {
                    "id": binds["id"],
                    "plugin_id": binds["pid"],
                    "version": binds["ver"],
                    "kind": binds["kind"],
                    "source_registry": binds["reg"],
                    "manifest": binds["man"],
                    "signature_verified": binds["sig"],
                    "installed_by": binds["installer"],
                    "installed_at": self.db._seq,
                }
            )
            self.rowcount = 1
        elif s.startswith("UPDATE installed_plugins"):
            hit = [r for r in self.db.rows if r["id"] == binds["id"]]
            for r in hit:
                r["signature_verified"] = binds["sig"]
            self.rowcount = len(hit)
        elif s.startswith("DELETE FROM installed_plugins"):
            before = len(self.db.rows)
            self.db.rows = [r for r in self.db.rows if r["id"] != binds["id"]]
            self.rowcount = before - len(self.db.rows)
        elif s.startswith("SELECT"):
            rows = self.db.rows
            if "WHERE id = :id" in s:
                rows = [r for r in rows if r["id"] == binds["id"]]
            elif "plugin_id = :pid AND version = :ver" in s:
                rows = [
                    r
                    for r in rows
                    if r["plugin_id"] == binds["pid"] and r["version"] == binds["ver"]
                ]
            elif "WHERE plugin_id = :pid" in s:
                rows = [r for r in rows if r["plugin_id"] == binds["pid"]]
            if "ORDER BY installed_at DESC" in s:
                rows = sorted(rows, key=lambda r: r["installed_at"], reverse=True)
            self._result = [self.db._row_tuple(r) for r in rows]
        else:  # pragma: no cover - 想定外 SQL は即失敗させる
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class FakeStoreConn:
    def __init__(self, db: FakeStoreDB):
        self.db = db
        self.committed = 0

    def cursor(self):
        return FakeStoreCursor(self.db)

    def commit(self):
        self.committed += 1


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeStoreDB()

    @contextlib.contextmanager
    def fake_connect():
        yield FakeStoreConn(db)

    monkeypatch.setattr(store, "connect", fake_connect)
    return db


# --- store CRUD --------------------------------------------------------------


def test_record_install_persists_and_returns_record(fake_db):
    rec = store.record_install(
        "user@example.com",
        _manifest(),
        source_registry="https://registry.example/jetuse",
        signature_verified=True,
    )
    assert rec["plugin_id"] == "acme/faq-summarizer"
    assert rec["version"] == "1.2.0"
    assert rec["kind"] == "usecase"
    assert rec["source_registry"] == "https://registry.example/jetuse"
    assert rec["signature_verified"] is True
    assert rec["installed_by"] == "user@example.com"
    assert rec["installed_at"] is not None
    # manifest は配布表現(camelCase)で往復できる。
    assert rec["manifest"]["schemaVersion"] == SCHEMA_VERSION
    assert rec["manifest"]["id"] == "acme/faq-summarizer"
    assert isinstance(rec["id"], str) and rec["id"]


def test_record_install_defaults_unverified_and_no_registry(fake_db):
    rec = store.record_install("u", _manifest())
    assert rec["signature_verified"] is False
    assert rec["source_registry"] is None


def test_get_install_roundtrip_and_miss(fake_db):
    rec = store.record_install("u", _manifest())
    got = store.get_install(rec["id"])
    assert got["id"] == rec["id"]
    assert got["manifest"]["id"] == "acme/faq-summarizer"
    assert store.get_install("does-not-exist") is None


def test_find_install_by_plugin_id_and_version(fake_db):
    store.record_install("u", _manifest(version="1.0.0"))
    store.record_install("u", _manifest(version="2.0.0"))
    found = store.find_install("acme/faq-summarizer", "2.0.0")
    assert found is not None
    assert found["version"] == "2.0.0"
    assert store.find_install("acme/faq-summarizer", "9.9.9") is None


def test_list_installs_all_and_filtered_newest_first(fake_db):
    store.record_install("u", _manifest(plugin_id="acme/a", version="1.0.0"))
    store.record_install("u", _manifest(plugin_id="acme/a", version="2.0.0"))
    store.record_install("u", _manifest(plugin_id="acme/b", version="1.0.0"))
    all_recs = store.list_installs()
    assert len(all_recs) == 3
    # 新しい順(後から入れた版が先頭)。
    assert all_recs[0]["version"] == "1.0.0" and all_recs[0]["plugin_id"] == "acme/b"
    only_a = store.list_installs("acme/a")
    assert {r["version"] for r in only_a} == {"1.0.0", "2.0.0"}
    assert all(r["plugin_id"] == "acme/a" for r in only_a)


def test_set_signature_verified_updates_and_reports_miss(fake_db):
    rec = store.record_install("u", _manifest())
    assert rec["signature_verified"] is False
    assert store.set_signature_verified(rec["id"], True) is True
    assert store.get_install(rec["id"])["signature_verified"] is True
    assert store.set_signature_verified("missing", True) is False


def test_delete_install(fake_db):
    rec = store.record_install("u", _manifest())
    assert store.delete_install(rec["id"]) is True
    assert store.get_install(rec["id"]) is None
    assert store.delete_install(rec["id"]) is False


def test_get_install_fail_soft_on_corrupt_manifest(fake_db):
    rec = store.record_install("u", _manifest())
    # DB 内の manifest CLOB が壊れていても read 経路は例外を投げず識別可能に返す。
    fake_db.rows[0]["manifest"] = "{not valid json"
    got = store.get_install(rec["id"])
    assert got is not None
    assert got["manifest"] is None
    assert got["manifest_error"] is True
    # list_installs も壊れた1件で全体が落ちない(fail-soft)。
    assert len(store.list_installs()) == 1


class _FakeLob:
    """oracledb の CLOB LOB オブジェクト(read() を持つ)を模す。"""

    def __init__(self, s: str):
        self._s = s

    def read(self) -> str:
        return self._s


def test_row_to_record_reads_clob_lob_object():
    # fetch_lobs=False でない構成で manifest が LOB で返っても read() して decode できる。
    payload = json.dumps({"id": "acme/x", "schemaVersion": "1"})
    row = ("id1", "acme/x", "1.0.0", "usecase", None, _FakeLob(payload), 1, "u", 5)
    rec = store._row_to_record(row)
    assert rec["manifest"]["id"] == "acme/x"
    assert rec["manifest_error"] is False


def test_record_install_validates_installed_by_and_registry(fake_db):
    with pytest.raises(ValueError):
        store.record_install("", _manifest())  # 空
    with pytest.raises(ValueError):
        store.record_install("   ", _manifest())  # 空白のみ
    with pytest.raises(ValueError):
        store.record_install("u" * 256, _manifest())  # 255 超
    with pytest.raises(ValueError):
        store.record_install("u", _manifest(), source_registry="r" * 256)  # 255 超
    # 境界(255)と None は通る。
    store.record_install("u" * 255, _manifest(version="1.0.0"), source_registry="r" * 255)
    store.record_install("u", _manifest(version="2.0.0"), source_registry=None)


def test_record_install_rejects_duplicate_version(fake_db):
    store.record_install("u", _manifest(version="1.0.0"))
    # 版固定スナップショットは (plugin_id, version) 一意。二重記録は DB が拒否する。
    with pytest.raises(_UniqueViolation):
        store.record_install("u", _manifest(version="1.0.0"))


# --- migration ---------------------------------------------------------------


def test_migration_files_define_expected_ddl():
    installed = (MIGRATIONS_DIR / "013_installed_plugins.sql").read_text()
    assert "CREATE TABLE installed_plugins" in installed
    for col in (
        "plugin_id",
        "version",
        "kind",
        "source_registry",
        "manifest",
        "signature_verified",
        "installed_by",
        "installed_at",
    ):
        assert col in installed
    assert "uq_installed_plugin_ver" in installed  # 版固定の一意制約
    assert "VARCHAR2(255)" in installed  # plugin_id は manifest id の上限と一致
    assert "VARCHAR2(64)" in installed  # version は manifest version の上限と一致
    # signature_verified は 0/1 のみ(store._row_to_record の bool 化を壊さない)。
    assert "CHECK (signature_verified IN (0, 1))" in installed

    uc_source = (MIGRATIONS_DIR / "014_usecase_source.sql").read_text()
    assert "ALTER TABLE usecases ADD" in uc_source
    ag_source = (MIGRATIONS_DIR / "015_agent_source.sql").read_text()
    assert "ALTER TABLE agents ADD" in ag_source
    for src in (uc_source, ag_source):
        assert "source_plugin_id" in src
        assert "source_version" in src


def test_manifest_length_bounds_fit_db_columns():
    """validator の長さ上限が installed_plugins のカラム幅以内であることを保証する。

    超えると「検証は通るが保存で桁超過する」valid manifest が生まれる(Codex 指摘の major)。
    """
    from jetuse_core.plugins.manifest import MAX_ID_LEN, MAX_VERSION_LEN

    installed = (MIGRATIONS_DIR / "013_installed_plugins.sql").read_text()
    assert f"plugin_id VARCHAR2({MAX_ID_LEN})" in installed
    assert f"version VARCHAR2({MAX_VERSION_LEN})" in installed
    for name in ("014_usecase_source.sql", "015_agent_source.sql"):
        source = (MIGRATIONS_DIR / name).read_text()
        assert f"source_plugin_id VARCHAR2({MAX_ID_LEN})" in source
        assert f"source_version VARCHAR2({MAX_VERSION_LEN})" in source


def test_alter_table_targets_exist_in_migration_set():
    """ALTER TABLE の対象表が同じ migration 群の CREATE TABLE に実在することを保証する。

    実 DB を使わずに「存在しない/綴り違いの表を ALTER する」クラスの誤り(ORA-00942)を
    検出する。014 が触る usecases/agents は 004/007 が作る、を機械的に確認する。
    """
    created: set[str] = set()
    altered: set[str] = set()
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        text = f.read_text()
        created |= {m.lower() for m in re.findall(r"CREATE TABLE\s+(\w+)", text, re.I)}
        altered |= {m.lower() for m in re.findall(r"ALTER TABLE\s+(\w+)", text, re.I)}
    assert {"usecases", "agents"} <= altered  # 014 が両表を ALTER している
    missing = altered - created
    assert not missing, f"ALTER 対象が CREATE されていない: {sorted(missing)}"


def test_migration_sql_splits_into_nonempty_statements():
    # ランナーは ';' で単文に割る。新規ファイルが空文を生まず割れることを確認。
    for name in (
        "013_installed_plugins.sql",
        "014_usecase_source.sql",
        "015_agent_source.sql",
    ):
        sql = (MIGRATIONS_DIR / name).read_text()
        stmts = mig._statements(sql)
        assert stmts and all(s.strip() for s in stmts)
    # 013 は CREATE TABLE + 2 INDEX のちょうど 3 文(末尾 ';' で空文を生まない)。
    stmts_013 = mig._statements((MIGRATIONS_DIR / "013_installed_plugins.sql").read_text())
    assert len(stmts_013) == 3


class FakeMigCursor:
    def __init__(self, state: dict):
        self.state = state
        self._result: list[tuple] = []

    def execute(self, sql: str, **binds):
        s = " ".join(sql.split())
        if "FROM user_tables" in s and "SCHEMA_MIGRATIONS" in s:
            self._result = [(1 if self.state["created"] else 0,)]
        elif s.startswith("CREATE TABLE schema_migrations"):
            self.state["created"] = True
        elif s.startswith("SELECT version FROM schema_migrations"):
            self._result = [(v,) for v in sorted(self.state["applied"])]
        elif s.startswith("INSERT INTO schema_migrations"):
            self.state["applied"].add(binds["v"])
        else:
            # 実 DDL は fake では no-op(構文の実機検証は人間ゲート)。
            self.state["ddl"].append(s)

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class FakeMigConn:
    def __init__(self, state: dict):
        self.state = state

    def cursor(self):
        return FakeMigCursor(self.state)

    def commit(self):
        pass


class FakeMigPool:
    def __init__(self, state: dict):
        self.state = state

    @contextlib.contextmanager
    def acquire(self):
        yield FakeMigConn(self.state)


def test_migrate_applies_new_migrations_then_idempotent(monkeypatch):
    state = {"created": False, "applied": set(), "ddl": []}
    monkeypatch.setattr(mig, "get_pool", lambda: FakeMigPool(state))

    first = mig.migrate()
    assert "013_installed_plugins" in first
    assert "014_usecase_source" in first
    assert "015_agent_source" in first

    # 再適用は何も適用せず(冪等)、例外も出ない。
    second = mig.migrate()
    assert second == []
