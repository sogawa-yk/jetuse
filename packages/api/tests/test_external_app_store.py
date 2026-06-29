"""external-app の登録（external_app_store）＋ migration の単体テスト（ASSET-01 / BE-06）。

実 ADB には接続せず、`external_app_instances` を再現するインメモリの fake 接続で登録→取得→一覧→
削除→出所削除を往復させる。**実シークレット値（client_secret / id_token）が保存されない**
（参照名 clientIdRef/secretRef のみ残る）ことも検証する。実機（loop ADB）の適用確認は E2E に委ねる。
"""

import contextlib
import json
import pathlib

import pytest

import jetuse_core.migrate as mig
from jetuse_core.plugins import external_app_store
from jetuse_core.plugins.denpyon_external_app import denpyon_external_app_manifest
from jetuse_core.plugins.manifest import SCHEMA_VERSION, validate_manifest

MIGRATIONS_DIR = pathlib.Path(mig.__file__).parent / "migrations"

URL = "https://denpyon.example.com/app"
ISSUER = "https://idp.example.com"
AUDIENCE = "https://denpyon.example.com"


def _manifest():
    return denpyon_external_app_manifest(url=URL, issuer=ISSUER, audience=AUDIENCE)


# --- fake 接続 ----------------------------------------------------------------

_INSTANCE_ORDER = [
    "id",
    "plugin_id",
    "source_version",
    "name",
    "app",
    "embed",
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
        if s.startswith("INSERT INTO external_app_instances"):
            self.db._seq += 1
            self.db.instances.append(
                {
                    "id": binds["id"],
                    "plugin_id": binds["pid"],
                    "source_version": binds["ver"],
                    "name": binds["name"],
                    "app": binds["app"],
                    "embed": binds["embed"],
                    "definition": binds["defn"],
                    "registered_by": binds["registrar"],
                    "created_at": self.db._seq,
                }
            )
            self.rowcount = 1
        elif s.startswith("DELETE FROM external_app_instances"):
            before = len(self.db.instances)
            if "WHERE id = :id" in s:
                self.db.instances = [r for r in self.db.instances if r["id"] != binds["id"]]
            else:  # 出所キー削除
                self.db.instances = [
                    r
                    for r in self.db.instances
                    if not (r["plugin_id"] == binds["pid"] and r["source_version"] == binds["ver"])
                ]
            self.rowcount = before - len(self.db.instances)
        elif s.startswith("SELECT") and "FROM external_app_instances" in s:
            rows = self.db.instances
            if "WHERE id = :id" in s:
                rows = [r for r in rows if r["id"] == binds["id"]]
            else:
                if "plugin_id = :pid" in s:
                    rows = [r for r in rows if r["plugin_id"] == binds["pid"]]
                if "app = :app" in s:
                    rows = [r for r in rows if r["app"] == binds["app"]]
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

    monkeypatch.setattr(external_app_store, "connect", fake_connect)
    return db


# --- 登録往復 ----------------------------------------------------------------


def test_register_and_get(fake_db):
    rec = external_app_store.register_external_app(_manifest(), registered_by="sa@example.com")
    assert rec["plugin_id"] == "jetuse/denpyon-external-app"
    assert rec["app"] == "denpyon"
    assert rec["embed"] == "iframe"
    assert rec["definition"]["sso"]["secretRef"] == "denpyon-oidc-client-secret"

    got = external_app_store.get_external_app(rec["id"])
    assert got is not None
    assert got["app"] == "denpyon"
    # 実シークレット値は保存されない（参照名のみ残る）。
    stored = json.dumps(got, ensure_ascii=False)
    assert "denpyon-oidc-client-secret" in stored  # 参照名は残る
    assert "client_secret" not in stored  # 実値を運ぶキーが無い
    assert "id_token" not in stored


def test_register_persists_definition_camel(fake_db):
    external_app_store.register_external_app(_manifest(), registered_by="sa")
    parsed = json.loads(fake_db.instances[0]["definition"])
    assert parsed["sso"]["clientIdRef"] == "denpyon-oidc-client-id"
    assert parsed["embed"] == "iframe"


def test_list_and_filter(fake_db):
    external_app_store.register_external_app(_manifest(), registered_by="sa")
    rows = external_app_store.list_external_apps()
    assert len(rows) == 1
    assert external_app_store.list_external_apps(app="denpyon")[0]["app"] == "denpyon"
    assert external_app_store.list_external_apps(app="nope") == []


def test_remove_and_delete_by_source(fake_db):
    rec = external_app_store.register_external_app(_manifest(), registered_by="sa")
    assert external_app_store.remove_external_app(rec["id"]) is True
    assert external_app_store.get_external_app(rec["id"]) is None
    assert external_app_store.remove_external_app(rec["id"]) is False

    rec2 = external_app_store.register_external_app(_manifest(), registered_by="sa")
    deleted = external_app_store.delete_by_source(rec2["plugin_id"], rec2["source_version"])
    assert deleted == 1
    assert external_app_store.list_external_apps() == []


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
        external_app_store.register_external_app(m, registered_by="sa")
    assert fake_db.instances == []


def test_register_requires_registered_by(fake_db):
    with pytest.raises(ValueError):
        external_app_store.register_external_app(_manifest(), registered_by="  ")
    assert fake_db.instances == []


# --- migration -------------------------------------------------------------


def _strip_sql_comments(sql: str) -> str:
    return "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )


def test_migration_file_present_and_well_formed():
    f = MIGRATIONS_DIR / "026_external_app_instances.sql"
    assert f.exists()
    stmts = mig._statements(_strip_sql_comments(f.read_text()))
    assert sum(1 for s in stmts if s.upper().startswith("CREATE TABLE")) == 1
    assert sum(1 for s in stmts if s.upper().startswith("CREATE INDEX")) == 2
    ddl = " ".join(stmts).upper()
    assert "EXTERNAL_APP_INSTANCES" in ddl
    # 認証実値の列を持たない（DDL 本体に secret/token/password の列が無い）。
    assert "SECRET" not in ddl
    assert "TOKEN" not in ddl
    assert "PASSWORD" not in ddl
