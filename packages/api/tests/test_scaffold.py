"""sample-app の scaffold 取込ロジック＋migration の単体テスト(SBA-01)。

実 ADB には接続せず、`sample_app_instances` / `sample_app_seed_rows` を再現するインメモリの
fake 接続で展開→取得→削除を往復させる。合成バリデーションが不足を検出したら DB に何も書かない
(fail-closed)ことも検証する。実機(ローカル/loop ADB)での適用確認は E2E に委ねる。
"""

import contextlib
import json
import pathlib

import pytest

import jetuse_core.migrate as mig
from jetuse_core.plugins import scaffold
from jetuse_core.plugins.manifest import (
    MAX_ID_LEN,
    MAX_VERSION_LEN,
    SCHEMA_VERSION,
    validate_manifest,
)
from jetuse_core.plugins.sample_app import CompositionError

MIGRATIONS_DIR = pathlib.Path(mig.__file__).parent / "migrations"


def _definition() -> dict:
    return {
        "summary": "問い合わせ管理",
        "datasets": [
            {
                "name": "tickets",
                "fields": [
                    {"name": "subject", "type": "string", "required": True},
                    {"name": "category", "type": "string"},
                ],
                "seed": [
                    {"subject": "ログイン不可", "category": "認証"},
                    {"subject": "請求", "category": "請求"},
                ],
            },
            {
                "name": "agents_roster",
                "fields": [{"name": "name", "type": "string"}],
                "seed": [{"name": "山田"}],
            },
        ],
        "aiSlots": [
            {
                "key": "faq-answer",
                "title": "FAQ回答",
                "capability": "rag.search",
                "permissions": ["platform:rag.search"],
            },
            {"key": "auto-classify", "title": "自動分類", "capability": "classify"},
        ],
        "screens": [
            {
                "key": "inbox",
                "title": "受信一覧",
                "type": "list",
                "dataset": "tickets",
                "slots": ["auto-classify", "faq-answer"],
            }
        ],
    }


def _manifest(version="1.0.0", permissions=None):
    return validate_manifest(
        {
            "schemaVersion": SCHEMA_VERSION,
            "id": "jetuse/support-desk",
            "version": version,
            "kind": "sample-app",
            "name": "問い合わせ管理",
            "publisher": "jetuse",
            "jetuse": {"minVersion": "0.3.0"},
            "permissions": ["platform:rag.search"] if permissions is None else permissions,
            "contributes": {"sample-app": _definition()},
        }
    )


# --- fake 接続 ----------------------------------------------------------------

_INSTANCE_ORDER = [
    "id",
    "plugin_id",
    "source_version",
    "name",
    "definition",
    "created_by",
    "created_at",
]


class FakeDB:
    def __init__(self):
        self.instances: list[dict] = []
        self.seed_rows: list[dict] = []
        self._seq = 0


class FakeCursor:
    def __init__(self, db: FakeDB):
        self.db = db
        self.rowcount = 0
        self._result: list[tuple] = []

    def execute(self, sql: str, **binds):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO sample_app_instances"):
            self.db._seq += 1
            self.db.instances.append(
                {
                    "id": binds["id"],
                    "plugin_id": binds["pid"],
                    "source_version": binds["ver"],
                    "name": binds["name"],
                    "definition": binds["defn"],
                    "created_by": binds["creator"],
                    "created_at": self.db._seq,
                }
            )
            self.rowcount = 1
        elif s.startswith("DELETE FROM sample_app_seed_rows"):
            before = len(self.db.seed_rows)
            self.db.seed_rows = [
                r for r in self.db.seed_rows if r["instance_id"] != binds["iid"]
            ]
            self.rowcount = before - len(self.db.seed_rows)
        elif s.startswith("DELETE FROM sample_app_instances"):
            before = len(self.db.instances)
            self.db.instances = [r for r in self.db.instances if r["id"] != binds["id"]]
            self.rowcount = before - len(self.db.instances)
        elif s.startswith("SELECT") and "FROM sample_app_instances" in s:
            rows = self.db.instances
            if "WHERE id = :id" in s:
                rows = [r for r in rows if r["id"] == binds["id"]]
            elif "WHERE plugin_id = :pid" in s:
                rows = [r for r in rows if r["plugin_id"] == binds["pid"]]
            if "ORDER BY created_at DESC" in s:
                rows = sorted(rows, key=lambda r: r["created_at"], reverse=True)
            self._result = [tuple(r[c] for c in _INSTANCE_ORDER) for r in rows]
        elif s.startswith("SELECT") and "FROM sample_app_seed_rows" in s:
            rows = [r for r in self.db.seed_rows if r["instance_id"] == binds["iid"]]
            if "AND dataset = :ds" in s:
                rows = [r for r in rows if r["dataset"] == binds["ds"]]
            rows = sorted(rows, key=lambda r: (r["dataset"], r["row_index"]))
            self._result = [
                (r["id"], r["dataset"], r["row_index"], r["payload"]) for r in rows
            ]
        else:  # pragma: no cover
            raise AssertionError(f"unexpected SQL: {s}")

    def executemany(self, sql: str, seq):
        s = " ".join(sql.split())
        assert s.startswith("INSERT INTO sample_app_seed_rows"), s
        for tup in seq:
            self.db.seed_rows.append(
                {
                    "id": tup[0],
                    "instance_id": tup[1],
                    "dataset": tup[2],
                    "row_index": tup[3],
                    "payload": tup[4],
                }
            )
        self.rowcount = len(seq)

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class FakeConn:
    def __init__(self, db: FakeDB):
        self.db = db
        self.committed = 0

    def cursor(self):
        return FakeCursor(self.db)

    def commit(self):
        self.committed += 1


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeDB()

    @contextlib.contextmanager
    def fake_connect():
        yield FakeConn(db)

    monkeypatch.setattr(scaffold, "connect", fake_connect)
    return db


# --- scaffold 展開 ------------------------------------------------------------


def test_scaffold_expands_definition_and_seed(fake_db):
    rec = scaffold.scaffold_sample_app(_manifest(), created_by="u@example.com")
    assert rec["plugin_id"] == "jetuse/support-desk"
    assert rec["source_version"] == "1.0.0"
    assert rec["name"] == "問い合わせ管理"
    # 定義が ADB に出現(camelCase で往復)。
    assert "aiSlots" in rec["definition"]
    assert [s["key"] for s in rec["definition"]["screens"]] == ["inbox"]
    # seed は tickets 2 + agents_roster 1 = 3 行展開。
    assert rec["seed_count"] == 3
    assert len(fake_db.seed_rows) == 3
    # 合成バリデーション結果が添付される。
    assert rec["composition"]["ok"] is True
    assert set(rec["composition"]["required_capabilities"]) == {"rag.search", "classify"}


def test_scaffold_seed_rows_readable_and_ordered(fake_db):
    rec = scaffold.scaffold_sample_app(_manifest(), created_by="u")
    rows = scaffold.list_seed_rows(rec["id"])
    assert [r["dataset"] for r in rows] == ["agents_roster", "tickets", "tickets"]
    tickets = scaffold.list_seed_rows(rec["id"], dataset="tickets")
    assert [r["row_index"] for r in tickets] == [0, 1]
    assert tickets[0]["payload"]["subject"] == "ログイン不可"


def test_scaffold_get_and_list(fake_db):
    rec = scaffold.scaffold_sample_app(_manifest(version="1.0.0"), created_by="u")
    scaffold.scaffold_sample_app(_manifest(version="2.0.0"), created_by="u")
    got = scaffold.get_instance(rec["id"])
    assert got["definition"]["summary"] == "問い合わせ管理"
    all_recs = scaffold.list_instances()
    assert len(all_recs) == 2
    # 新しい順(後から入れた版が先頭)。
    assert all_recs[0]["source_version"] == "2.0.0"
    filtered = scaffold.list_instances("jetuse/support-desk")
    assert {r["source_version"] for r in filtered} == {"1.0.0", "2.0.0"}
    assert scaffold.get_instance("missing") is None


def test_scaffold_delete_cascades_seed(fake_db):
    rec = scaffold.scaffold_sample_app(_manifest(), created_by="u")
    assert scaffold.delete_instance(rec["id"]) is True
    assert scaffold.get_instance(rec["id"]) is None
    assert scaffold.list_seed_rows(rec["id"]) == []
    assert scaffold.delete_instance(rec["id"]) is False


def test_scaffold_refuses_when_capability_missing(fake_db):
    """合成バリデーションが必要ケイパビリティ不足を検出 → DB に何も書かず CompositionError。"""
    with pytest.raises(CompositionError) as exc:
        scaffold.scaffold_sample_app(
            _manifest(), created_by="u", available_capabilities={"rag.search"}
        )
    assert exc.value.report.missing_capabilities == ["classify"]
    # fail-closed: インスタンスも seed も書かれない。
    assert fake_db.instances == []
    assert fake_db.seed_rows == []


def test_scaffold_refuses_when_permission_undeclared(fake_db):
    with pytest.raises(CompositionError) as exc:
        scaffold.scaffold_sample_app(_manifest(permissions=[]), created_by="u")
    assert exc.value.report.undeclared_permissions == ["platform:rag.search"]
    assert fake_db.instances == []


def test_scaffold_rejects_non_sample_app_manifest(fake_db):
    uc = validate_manifest(
        {
            "schemaVersion": SCHEMA_VERSION,
            "id": "acme/uc",
            "version": "1.0.0",
            "kind": "usecase",
            "name": "uc",
            "publisher": "p",
            "jetuse": {"minVersion": "0.1.0"},
            "contributes": {"usecase": {"template": "x"}},
        }
    )
    with pytest.raises(ValueError):
        scaffold.scaffold_sample_app(uc, created_by="u")


def test_scaffold_validates_created_by_and_name(fake_db):
    with pytest.raises(ValueError):
        scaffold.scaffold_sample_app(_manifest(), created_by="")
    with pytest.raises(ValueError):
        scaffold.scaffold_sample_app(_manifest(), created_by="   ")
    with pytest.raises(ValueError):
        scaffold.scaffold_sample_app(_manifest(), created_by="u" * 256)
    with pytest.raises(ValueError):
        scaffold.scaffold_sample_app(_manifest(), created_by="u", name="n" * 201)
    # 明示的に空/空白の name はサイレントに manifest.name へフォールバックせずエラー。
    with pytest.raises(ValueError):
        scaffold.scaffold_sample_app(_manifest(), created_by="u", name="")
    with pytest.raises(ValueError):
        scaffold.scaffold_sample_app(_manifest(), created_by="u", name="   ")
    # 名前上書きが効く。
    rec = scaffold.scaffold_sample_app(_manifest(), created_by="u", name="カスタム名")
    assert rec["name"] == "カスタム名"


def test_instance_record_fail_soft_on_corrupt_definition(fake_db):
    rec = scaffold.scaffold_sample_app(_manifest(), created_by="u")
    fake_db.instances[0]["definition"] = "{not valid json"
    got = scaffold.get_instance(rec["id"])
    assert got is not None
    assert got["definition"] is None
    assert got["definition_error"] is True


# --- migration ----------------------------------------------------------------


def test_migration_file_defines_expected_ddl():
    sql = (MIGRATIONS_DIR / "016_sample_app_instances.sql").read_text()
    assert "CREATE TABLE sample_app_instances" in sql
    assert "CREATE TABLE sample_app_seed_rows" in sql
    for col in ("plugin_id", "source_version", "name", "definition", "created_by"):
        assert col in sql
    # 出所追跡カラムの幅は manifest の上限と一致(検証済み定義が必ず保存できる)。
    assert f"plugin_id VARCHAR2({MAX_ID_LEN})" in sql
    assert f"source_version VARCHAR2({MAX_VERSION_LEN})" in sql
    # seed 行は instance 削除で連動削除。
    assert "ON DELETE CASCADE" in sql
    # 多バイト名のため name/created_by は CHAR セマンティクス(BYTE 既定環境での ORA-12899 回避)。
    assert "VARCHAR2(200 CHAR)" in sql
    assert "VARCHAR2(255 CHAR)" in sql


def test_migration_alter_targets_and_split():
    sql = (MIGRATIONS_DIR / "016_sample_app_instances.sql").read_text()
    stmts = mig._statements(sql)
    # CREATE TABLE x2 + CREATE INDEX x2 = 4 文(末尾 ';' で空文を生まない)。
    assert len(stmts) == 4
    assert all(s.strip() for s in stmts)
    # seed テーブルが instance テーブルより後に作られる(FK 参照順)。
    assert sql.index("CREATE TABLE sample_app_instances") < sql.index(
        "CREATE TABLE sample_app_seed_rows"
    )


def test_sample_app_migration_in_migrate_set(monkeypatch):
    """016 が migrate() の適用対象に含まれ、再適用で冪等(2 回目は空)。"""
    state = {"created": False, "applied": set(), "ddl": []}

    class FakeMigCursor:
        def __init__(self, st):
            self.state = st
            self._result = []

        def execute(self, sql, **binds):
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
                self.state["ddl"].append(s)

        def fetchone(self):
            return self._result[0] if self._result else None

        def fetchall(self):
            return list(self._result)

    class FakeMigConn:
        def __init__(self, st):
            self.state = st

        def cursor(self):
            return FakeMigCursor(self.state)

        def commit(self):
            pass

    class FakeMigPool:
        def __init__(self, st):
            self.state = st

        @contextlib.contextmanager
        def acquire(self):
            yield FakeMigConn(self.state)

    monkeypatch.setattr(mig, "get_pool", lambda: FakeMigPool(state))
    first = mig.migrate()
    assert "016_sample_app_instances" in first
    assert mig.migrate() == []


def test_definition_json_is_valid_json():
    """展開時に格納する definition CLOB が valid JSON であること(取り出し往復の前提)。"""
    from jetuse_core.plugins.sample_app import validate_sample_app

    d = validate_sample_app(_manifest())
    s = json.dumps(d.model_dump(by_alias=True), ensure_ascii=False)
    assert json.loads(s)["screens"][0]["key"] == "inbox"
