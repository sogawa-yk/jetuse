"""connector の登録(connector_store)＋migration の単体テスト(CON-01)。

実 ADB には接続せず、`connector_instances` を再現するインメモリの fake 接続で登録→取得→一覧→
削除を往復させる。合成バリデーション(権限スコープ宣言整合)が不整合を検出したら DB に何も書かない
(fail-closed)ことも検証する。実機(loop ADB)での適用確認は E2E に委ねる。
"""

import contextlib
import json
import pathlib

import pytest

import jetuse_core.migrate as mig
from jetuse_core.plugins import connector_store
from jetuse_core.plugins.connector import ConnectorCompositionError
from jetuse_core.plugins.manifest import SCHEMA_VERSION, validate_manifest

MIGRATIONS_DIR = pathlib.Path(mig.__file__).parent / "migrations"


def _definition(**over) -> dict:
    d = {
        "provider": "slack",
        "transport": "builtin",
        "auth": {"kind": "oauth2", "secretRef": "slack-bot-token", "scopes": ["chat:write"]},
        "actions": [
            {"name": "post_message", "title": "投稿"},
            {
                "name": "search_messages",
                "title": "検索",
                "permissions": ["platform:conversations.read"],
            },
        ],
    }
    d.update(over)
    return d


def _manifest(version="1.0.0", permissions=None, definition=None):
    return validate_manifest(
        {
            "schemaVersion": SCHEMA_VERSION,
            "id": "jetuse/slack-connector",
            "version": version,
            "kind": "connector",
            "name": "Slack コネクタ",
            "publisher": "jetuse",
            "jetuse": {"minVersion": "0.3.0"},
            "permissions": ["platform:conversations.read"]
            if permissions is None
            else permissions,
            "contributes": {
                "connector": _definition() if definition is None else definition
            },
        }
    )


# --- fake 接続 ----------------------------------------------------------------

_INSTANCE_ORDER = [
    "id",
    "plugin_id",
    "source_version",
    "name",
    "provider",
    "transport",
    "definition",
    "registered_by",
    "created_at",
]


class FakeDB:
    def __init__(self):
        self.instances: list[dict] = []
        self._seq = 0


class FakeCursor:
    def __init__(self, db: FakeDB):
        self.db = db
        self.rowcount = 0
        self._result: list[tuple] = []

    def execute(self, sql: str, **binds):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO connector_instances"):
            self.db._seq += 1
            self.db.instances.append(
                {
                    "id": binds["id"],
                    "plugin_id": binds["pid"],
                    "source_version": binds["ver"],
                    "name": binds["name"],
                    "provider": binds["prov"],
                    "transport": binds["trans"],
                    "definition": binds["defn"],
                    "registered_by": binds["registrar"],
                    "created_at": self.db._seq,
                }
            )
            self.rowcount = 1
        elif s.startswith("DELETE FROM connector_instances"):
            before = len(self.db.instances)
            self.db.instances = [r for r in self.db.instances if r["id"] != binds["id"]]
            self.rowcount = before - len(self.db.instances)
        elif s.startswith("SELECT") and "FROM connector_instances" in s:
            rows = self.db.instances
            if "WHERE id = :id" in s:
                rows = [r for r in rows if r["id"] == binds["id"]]
            else:
                if "plugin_id = :pid" in s:
                    rows = [r for r in rows if r["plugin_id"] == binds["pid"]]
                if "provider = :prov" in s:
                    rows = [r for r in rows if r["provider"] == binds["prov"]]
                if "ORDER BY created_at DESC" in s:
                    rows = sorted(rows, key=lambda r: r["created_at"], reverse=True)
            self._result = [tuple(r[c] for c in _INSTANCE_ORDER) for r in rows]
        else:  # pragma: no cover
            raise AssertionError(f"unexpected SQL: {s}")

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

    monkeypatch.setattr(connector_store, "connect", fake_connect)
    return db


# --- 登録往復 ----------------------------------------------------------------


def test_register_and_get(fake_db):
    rec = connector_store.register_connector(_manifest(), registered_by="sa@example.com")
    assert rec["plugin_id"] == "jetuse/slack-connector"
    assert rec["provider"] == "slack"
    assert rec["transport"] == "builtin"
    assert rec["definition"]["auth"]["secretRef"] == "slack-bot-token"
    assert rec["composition"]["ok"] is True

    got = connector_store.get_connector(rec["id"])
    assert got is not None
    assert got["provider"] == "slack"
    # 認証の実値は保存されない(参照名のみ)。
    stored = json.dumps(got, ensure_ascii=False)
    assert "slack-bot-token" in stored  # 参照名は残る
    assert "xoxb" not in stored  # 実トークン値は残らない


def test_register_persists_definition_camel(fake_db):
    connector_store.register_connector(_manifest(), registered_by="sa")
    raw = fake_db.instances[0]["definition"]
    parsed = json.loads(raw)
    assert parsed["auth"]["secretRef"] == "slack-bot-token"
    assert parsed["provider"] == "slack"


def test_list_and_filter(fake_db):
    connector_store.register_connector(_manifest(version="1.0.0"), registered_by="sa")
    teams_def = _definition(
        provider="teams", transport="mcp", endpoint="https://mcp.example.com/teams"
    )
    connector_store.register_connector(
        _manifest(version="1.1.0", definition=teams_def), registered_by="sa"
    )
    all_rows = connector_store.list_connectors()
    assert len(all_rows) == 2
    slack_only = connector_store.list_connectors(provider="slack")
    assert len(slack_only) == 1
    assert slack_only[0]["provider"] == "slack"


def test_remove(fake_db):
    rec = connector_store.register_connector(_manifest(), registered_by="sa")
    assert connector_store.remove_connector(rec["id"]) is True
    assert connector_store.get_connector(rec["id"]) is None
    assert connector_store.remove_connector(rec["id"]) is False


def test_register_fail_closed_on_undeclared(fake_db):
    # action が要求するスコープを宣言していない → 致命。DB に何も書かない。
    with pytest.raises(ConnectorCompositionError):
        connector_store.register_connector(
            _manifest(permissions=[]), registered_by="sa"
        )
    assert fake_db.instances == []


def test_register_rejects_wrong_kind(fake_db):
    m = validate_manifest(
        {
            "schemaVersion": SCHEMA_VERSION,
            "id": "jetuse/x",
            "version": "1.0.0",
            "kind": "agent",
            "name": "x",
            "publisher": "jetuse",
            "jetuse": {"minVersion": "0.3.0"},
            "contributes": {"agent": {"instructions": "hi"}},
        }
    )
    with pytest.raises(ValueError):
        connector_store.register_connector(m, registered_by="sa")
    assert fake_db.instances == []


def test_register_requires_registered_by(fake_db):
    with pytest.raises(ValueError):
        connector_store.register_connector(_manifest(), registered_by="  ")
    assert fake_db.instances == []


def test_explicit_blank_name_rejected(fake_db):
    with pytest.raises(ValueError):
        connector_store.register_connector(_manifest(), registered_by="sa", name="  ")
    assert fake_db.instances == []


# --- migration -------------------------------------------------------------


def _strip_sql_comments(sql: str) -> str:
    # `--` 行コメントを除いた DDL 本体だけを返す(コメント中の語に検査が反応しないように)。
    return "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )


def test_migration_file_present_and_well_formed():
    f = MIGRATIONS_DIR / "019_connector_instances.sql"
    assert f.exists()
    stmts = mig._statements(_strip_sql_comments(f.read_text()))
    # CREATE TABLE + 2 INDEX。
    assert sum(1 for s in stmts if s.upper().startswith("CREATE TABLE")) == 1
    assert sum(1 for s in stmts if s.upper().startswith("CREATE INDEX")) == 2
    ddl = " ".join(stmts).upper()
    assert "CONNECTOR_INSTANCES" in ddl
    # 認証実値の列を持たない(DDL 本体に secret/token/password の列が無い)。
    assert "SECRET" not in ddl
    assert "TOKEN" not in ddl
    assert "PASSWORD" not in ddl
