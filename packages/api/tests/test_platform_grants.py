"""Platform API スコープ承認＋発行フローのテスト(PAPI-02 / ADR-0014)。

純粋な承認ポリシー(validate_grant_scopes / select_issuable_scopes)と発行フロー(issue_token)の
正常系・境界・拒否系を網羅する。DB 永続化(approve_scopes / get_grant / revoke_grant の実書込)は
実 ADB の E2E(spikes/spike06_platform_grants.py)で確認する。本ユニットでは get_grant をスタブして
発行フローの分岐(no_grant / grant_revoked / scope_not_granted / 承認に閉じる)を検証する。
"""

import contextlib

import jwt
import pytest

import jetuse_core.db as jdb
from jetuse_core import platform_grants as pg
from jetuse_core.platform_broker import (
    AUDIENCE,
    ISSUER,
    BrokerDenied,
    verify_broker_token,
)
from jetuse_core.plugins.manifest import SCHEMA_VERSION, validate_manifest
from jetuse_core.settings import Settings

TENANT = "ocid1.tenancy.oc1..aaaa-tenant-A"
TENANT_B = "ocid1.tenancy.oc1..bbbb-tenant-B"
PLUGIN = "acme/faq-summarizer"


def _settings(secret: str = "spike-broker-secret-32bytes-min!!", ttl: int = 300) -> Settings:
    return Settings(platform_broker_secret=secret, platform_token_ttl_seconds=ttl)


def _manifest(permissions=None):
    return validate_manifest(
        {
            "schemaVersion": SCHEMA_VERSION,
            "id": PLUGIN,
            "version": "1.2.0",
            "kind": "usecase",
            "name": "FAQ要約",
            "publisher": "acme-corp",
            "jetuse": {"minVersion": "0.3.0"},
            "permissions": ["platform:rag.search", "platform:db.query"]
            if permissions is None
            else permissions,
            "contributes": {
                "usecase": {
                    "fields": [{"name": "text", "type": "textarea"}],
                    "template": "要約して: {{text}}",
                }
            },
        }
    )


# --- validate_grant_scopes(承認の純粋ポリシー) -------------------------------


def test_validate_grant_scopes_subset_of_manifest_permissions():
    m = _manifest()  # 要求: rag.search, db.query
    granted = pg.validate_grant_scopes(m, ["platform:rag.search"])
    assert granted == frozenset({"platform:rag.search"})


def test_validate_grant_scopes_all_declared_ok():
    m = _manifest()
    granted = pg.validate_grant_scopes(
        m, ["platform:rag.search", "platform:db.query"]
    )
    assert granted == frozenset({"platform:rag.search", "platform:db.query"})


def test_validate_grant_scopes_empty_rejected():
    with pytest.raises(pg.GrantError):
        pg.validate_grant_scopes(_manifest(), [])


def test_validate_grant_scopes_unknown_rejected():
    with pytest.raises(pg.GrantError):
        pg.validate_grant_scopes(_manifest(), ["platform:not-a-scope"])


def test_validate_grant_scopes_not_requested_by_manifest_rejected():
    # manifest が rag.search しか要求していないのに db.query を承認しようとする → 拒否(最小権限)。
    m = _manifest(permissions=["platform:rag.search"])
    with pytest.raises(pg.GrantError):
        pg.validate_grant_scopes(m, ["platform:db.query"])


def test_validate_grant_scopes_empty_permissions_cannot_grant():
    m = _manifest(permissions=[])
    with pytest.raises(pg.GrantError):
        pg.validate_grant_scopes(m, ["platform:rag.search"])


# --- select_issuable_scopes(発行スコープ選択の純粋ポリシー) -------------------


def test_select_issuable_default_is_full_grant():
    granted = frozenset({"platform:rag.search", "platform:db.query"})
    assert pg.select_issuable_scopes(granted, None) == granted


def test_select_issuable_subset_request_ok():
    granted = frozenset({"platform:rag.search", "platform:db.query"})
    assert pg.select_issuable_scopes(granted, ["platform:rag.search"]) == frozenset(
        {"platform:rag.search"}
    )


def test_select_issuable_excess_request_denied():
    granted = frozenset({"platform:rag.search"})
    with pytest.raises(pg.GrantDenied) as e:
        pg.select_issuable_scopes(granted, ["platform:db.query"])
    assert e.value.reason == "scope_not_granted"


def test_select_issuable_empty_request_denied():
    with pytest.raises(pg.GrantDenied) as e:
        pg.select_issuable_scopes(frozenset({"platform:rag.search"}), [])
    assert e.value.reason == "empty_request"


# --- issue_token(発行フロー。get_grant をスタブ) ----------------------------


def _stub_grant(monkeypatch, *, scopes, status=pg.GRANT_STATUS_ACTIVE, exists=True):
    def fake_get_grant(tenant, plugin_id):
        if not exists or tenant != TENANT or plugin_id != PLUGIN:
            return None
        return {
            "id": "g-1",
            "tenant": TENANT,
            "plugin_id": PLUGIN,
            "source_version": "1.2.0",
            "scopes": sorted(scopes),
            "status": status,
            "approved_by": "sa@example.com",
            "created_at": "2026-06-27T00:00:00+00:00",
            "updated_at": "2026-06-27T00:00:00+00:00",
        }

    monkeypatch.setattr(pg, "get_grant", fake_get_grant)


def test_issue_token_carries_only_granted_scopes(monkeypatch):
    # manifest が db.query を宣言しても、承認は rag.search のみ → トークンに db.query は載らない。
    _stub_grant(monkeypatch, scopes=["platform:rag.search"])
    s = _settings()
    token = pg.issue_token(TENANT, PLUGIN, settings=s)
    ctx = verify_broker_token(token, settings=s)
    assert ctx.scopes == frozenset({"platform:rag.search"})
    assert ctx.tenant == TENANT
    assert ctx.plugin_id == PLUGIN
    claims = jwt.decode(token, options={"verify_signature": False}, audience=AUDIENCE)
    assert claims["iss"] == ISSUER
    assert claims["scope"] == "platform:rag.search"


def test_issue_token_subset_request_ok(monkeypatch):
    _stub_grant(monkeypatch, scopes=["platform:rag.search", "platform:db.query"])
    s = _settings()
    token = pg.issue_token(
        TENANT, PLUGIN, scopes=["platform:db.query"], settings=s
    )
    ctx = verify_broker_token(token, settings=s)
    assert ctx.scopes == frozenset({"platform:db.query"})


def test_issue_token_excess_request_denied_no_token(monkeypatch):
    # 承認 rag.search のみ。db.query を要求 → scope_not_granted で発行されない(fail-closed)。
    _stub_grant(monkeypatch, scopes=["platform:rag.search"])
    with pytest.raises(pg.GrantDenied) as e:
        pg.issue_token(
            TENANT, PLUGIN, scopes=["platform:db.query"], settings=_settings()
        )
    assert e.value.reason == "scope_not_granted"


def test_issue_token_no_grant_denied(monkeypatch):
    _stub_grant(monkeypatch, scopes=["platform:rag.search"])
    with pytest.raises(pg.GrantDenied) as e:
        pg.issue_token(TENANT_B, PLUGIN, settings=_settings())  # 別テナント = グラント無し
    assert e.value.reason == "no_grant"


def test_issue_token_revoked_grant_denied(monkeypatch):
    _stub_grant(
        monkeypatch, scopes=["platform:rag.search"], status=pg.GRANT_STATUS_REVOKED
    )
    with pytest.raises(pg.GrantDenied) as e:
        pg.issue_token(TENANT, PLUGIN, settings=_settings())
    assert e.value.reason == "grant_revoked"


def test_issue_token_retired_scope_in_grant_fails_closed(monkeypatch):
    # 承認に語彙外スコープが残っていたら broker(認可コア)が未知スコープとして弾く(fail-closed)。
    _stub_grant(monkeypatch, scopes=["platform:retired-scope"])
    with pytest.raises(BrokerDenied) as e:  # 認可コアが未知スコープとして弾く
        pg.issue_token(TENANT, PLUGIN, settings=_settings())
    assert e.value.reason == "unknown_scope"


# --- 永続化(approve_scopes / get_grant / list_grants / revoke_grant) ---------
# 実 ADB には接続せず、platform_scope_grants を再現するインメモリ fake 接続で承認→取得→一覧→失効を
# 往復させる(connector_store テストと同方式)。MERGE/UPDATE の SQL セマンティクスは E2E で確認する。

_GRANT_ORDER = [
    "id",
    "tenant",
    "plugin_id",
    "source_version",
    "scopes",
    "status",
    "approved_by",
    "created_at",
    "updated_at",
]


class _FakeDB:
    def __init__(self):
        self.rows: list[dict] = []
        self._seq = 0


class _FakeCursor:
    def __init__(self, db):
        self.db = db
        self.rowcount = 0
        self._result: list[tuple] = []

    def _find(self, tenant, pid):
        for r in self.db.rows:
            if r["tenant"] == tenant and r["plugin_id"] == pid:
                return r
        return None

    def execute(self, sql: str, **b):
        s = " ".join(sql.split())
        if s.startswith("MERGE INTO platform_scope_grants"):
            existing = self._find(b["tenant"], b["pid"])
            self.db._seq += 1
            if existing:  # upsert: 更新(created_at 保持・updated_at 前進・status を ACTIVE へ)
                existing.update(
                    source_version=b["ver"],
                    scopes=b["scopes"],
                    status=b["active"],
                    approved_by=b["approver"],
                    updated_at=self.db._seq,
                )
            else:
                self.db.rows.append(
                    {
                        "id": b["id"],
                        "tenant": b["tenant"],
                        "plugin_id": b["pid"],
                        "source_version": b["ver"],
                        "scopes": b["scopes"],
                        "status": b["active"],
                        "approved_by": b["approver"],
                        "created_at": self.db._seq,
                        "updated_at": self.db._seq,
                    }
                )
            self.rowcount = 1
        elif s.startswith("UPDATE platform_scope_grants"):
            r = self._find(b["tenant"], b["pid"])
            if r and r["status"] == b["active"]:
                self.db._seq += 1
                r["status"] = b["revoked"]
                r["updated_at"] = self.db._seq
                self.rowcount = 1
            else:
                self.rowcount = 0
        elif s.startswith("SELECT") and "FROM platform_scope_grants" in s:
            rows = self.db.rows
            if "tenant = :tenant" in s and "tenant" in b:
                rows = [r for r in rows if r["tenant"] == b["tenant"]]
            if "plugin_id = :pid" in s and "pid" in b:
                rows = [r for r in rows if r["plugin_id"] == b["pid"]]
            if "status = :status" in s and "status" in b:
                rows = [r for r in rows if r["status"] == b["status"]]
            if "ORDER BY updated_at DESC" in s:
                rows = sorted(rows, key=lambda r: r["updated_at"], reverse=True)
            self._result = [tuple(r[c] for c in _GRANT_ORDER) for r in rows]
        else:  # pragma: no cover
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class _FakeConn:
    def __init__(self, db):
        self.db = db
        self.committed = 0

    def cursor(self):
        return _FakeCursor(self.db)

    def commit(self):
        self.committed += 1


@pytest.fixture
def fake_db(monkeypatch):
    db = _FakeDB()

    @contextlib.contextmanager
    def fake_connect():
        yield _FakeConn(db)

    # platform_grants は関数内で from .db import connect する。jetuse_core.db.connect を差し替える。
    monkeypatch.setattr(jdb, "connect", fake_connect)
    return db


def test_approve_persists_active_grant(fake_db):
    rec = pg.approve_scopes(
        _manifest(), tenant=TENANT, scopes=["platform:rag.search"], approved_by="sa@x"
    )
    assert rec["status"] == pg.GRANT_STATUS_ACTIVE
    assert rec["scopes"] == ["platform:rag.search"]  # db.query は宣言されても未承認なので載らない
    assert rec["source_version"] == "1.2.0"
    got = pg.get_grant(TENANT, PLUGIN)
    assert got["id"] == rec["id"]
    assert got["scopes"] == ["platform:rag.search"]


def test_reapprove_upserts_same_row(fake_db):
    r1 = pg.approve_scopes(
        _manifest(), tenant=TENANT, scopes=["platform:rag.search"], approved_by="sa@x"
    )
    r2 = pg.approve_scopes(
        _manifest(),
        tenant=TENANT,
        scopes=["platform:rag.search", "platform:db.query"],
        approved_by="sa2@x",
    )
    assert len(fake_db.rows) == 1  # upsert: 行は増えない
    assert r2["id"] == r1["id"]  # created_at 由来の id を保持
    assert r2["scopes"] == ["platform:db.query", "platform:rag.search"]
    assert r2["approved_by"] == "sa2@x"


def test_get_grant_absent_returns_none(fake_db):
    assert pg.get_grant(TENANT, PLUGIN) is None


def test_revoke_then_issue_denied(fake_db):
    pg.approve_scopes(
        _manifest(), tenant=TENANT, scopes=["platform:rag.search"], approved_by="sa@x"
    )
    assert pg.revoke_grant(TENANT, PLUGIN) is True
    assert pg.get_grant(TENANT, PLUGIN)["status"] == pg.GRANT_STATUS_REVOKED
    # 失効後の二重失効は冪等に False。
    assert pg.revoke_grant(TENANT, PLUGIN) is False
    # 失効後は発行フローが grant_revoked で拒否(get_grant は実 fake 経由)。
    with pytest.raises(pg.GrantDenied) as e:
        pg.issue_token(TENANT, PLUGIN, settings=_settings())
    assert e.value.reason == "grant_revoked"


def test_issue_token_after_real_approve(fake_db):
    # 承認 → 発行 → 検証 の往復(get_grant をスタブせず fake DB 経由で通す)。
    pg.approve_scopes(
        _manifest(), tenant=TENANT, scopes=["platform:rag.search"], approved_by="sa@x"
    )
    s = _settings()
    token = pg.issue_token(TENANT, PLUGIN, settings=s)
    ctx = verify_broker_token(token, settings=s)
    assert ctx.scopes == frozenset({"platform:rag.search"})


def test_list_grants_filters_by_status(fake_db):
    pg.approve_scopes(
        _manifest(), tenant=TENANT, scopes=["platform:rag.search"], approved_by="sa@x"
    )
    pg.approve_scopes(
        _manifest(),
        tenant=TENANT_B,
        scopes=["platform:db.query"],
        approved_by="sa@x",
    )
    pg.revoke_grant(TENANT_B, PLUGIN)
    active = pg.list_grants(status=pg.GRANT_STATUS_ACTIVE)
    assert {g["tenant"] for g in active} == {TENANT}
    assert len(pg.list_grants(plugin_id=PLUGIN)) == 2


def test_approve_rejects_whitespace_tenant(fake_db):
    # F-002: 前後空白付き tenant は割れの原因なので承認時に拒否する。
    with pytest.raises(pg.GrantError):
        pg.approve_scopes(
            _manifest(),
            tenant=f" {TENANT} ",
            scopes=["platform:rag.search"],
            approved_by="sa@x",
        )


def test_approve_rejects_scope_not_in_manifest(fake_db):
    # manifest が rag.search のみ要求 → db.query の承認は拒否(DB に何も書かない)。
    m = _manifest(permissions=["platform:rag.search"])
    with pytest.raises(pg.GrantError):
        pg.approve_scopes(
            m, tenant=TENANT, scopes=["platform:db.query"], approved_by="sa@x"
        )
    assert fake_db.rows == []
