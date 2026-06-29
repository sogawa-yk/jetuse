"""スコープ承認 REST ルート(BE-05 / PAPI-02 の到達経路)の API テスト。

approve / revoke / list / candidates の SA 限定・二重閉包(fail-closed)・監査(誰が/いつ/どの scope)を
検証する。DB 永続化(approve_scopes / get_grant / revoke_grant の実書込)と監査の実 INSERT は実 ADB の
E2E で確認するため、ここでは store / platform_grants / 監査を捕捉して DB に依存させない。
"""

import pytest
from fastapi.testclient import TestClient

from jetuse_core import platform_broker as pb
from jetuse_core import platform_grants as pg
from jetuse_core.plugins import store as plugin_store
from jetuse_core.plugins.manifest import SCHEMA_VERSION
from jetuse_core.settings import get_settings
from service.main import app

TENANT = "ocid1.tenancy.oc1..aaaa-tenant-A"
PLUGIN = "acme/faq-summarizer"
DB_QUERY = "platform:db.query"
RAG_SEARCH = "platform:rag.search"
CONNECTOR_INVOKE = "platform:connector.invoke"


@pytest.fixture(autouse=True)
def reset_settings():
    # is_admin は module-level get_settings()(lru_cache)を読む。env 差し替え時はキャッシュを捨てる。
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _manifest_dict(permissions=(DB_QUERY, RAG_SEARCH)):
    return {
        "schemaVersion": SCHEMA_VERSION,
        "id": PLUGIN,
        "version": "1.2.0",
        "kind": "usecase",
        "name": "FAQ要約",
        "publisher": "acme-corp",
        "jetuse": {"minVersion": "0.3.0"},
        "permissions": list(permissions),
        "contributes": {
            "usecase": {
                "fields": [{"name": "text", "type": "textarea"}],
                "template": "要約して: {{text}}",
            }
        },
    }


@pytest.fixture
def audit(monkeypatch):
    """承認/失効/拒否の監査行を捕捉する(実 DB を触らせない)。"""
    records: list[dict] = []
    monkeypatch.setattr(pb, "record_broker_access", lambda **kw: records.append(kw))
    return records


@pytest.fixture
def install(monkeypatch):
    """インストール済み manifest を返すよう store をスタブする(plugin_id の最新版)。"""

    def _list_installs(plugin_id=None):
        return [
            {
                "plugin_id": PLUGIN,
                "version": "1.2.0",
                "manifest": _manifest_dict(),
                "manifest_error": False,
            }
        ]

    def _find_install(plugin_id, version):
        return _list_installs(plugin_id)[0] if version == "1.2.0" else None

    monkeypatch.setattr(plugin_store, "list_installs", _list_installs)
    monkeypatch.setattr(plugin_store, "find_install", _find_install)


def _client(monkeypatch, *, admin: bool = True) -> TestClient:
    # AUTH_REQUIRED 既定 false → require_user は dev-user。ADMIN_USERS に載せて SA にする。
    monkeypatch.setenv("ADMIN_USERS", "dev-user" if admin else "someone-else")
    get_settings.cache_clear()
    return TestClient(app)


# --- approve(承認 / 二重閉包 / 監査) ----------------------------------------


def test_approve_persists_within_manifest_and_audits(install, audit, monkeypatch):
    captured = {}

    def fake_approve(manifest, *, tenant, scopes, approved_by):
        captured.update(
            tenant=tenant, scopes=sorted(scopes), approved_by=approved_by,
            permissions=sorted(manifest.permissions),
        )
        return {
            "id": "g1", "tenant": tenant, "plugin_id": PLUGIN,
            "source_version": "1.2.0", "scopes": sorted(scopes),
            "status": "ACTIVE", "approved_by": approved_by,
            "created_at": "t", "updated_at": "t",
        }

    monkeypatch.setattr(pg, "approve_scopes", fake_approve)
    res = _client(monkeypatch).post(
        "/platform/grants",
        json={"tenant": TENANT, "plugin_id": PLUGIN, "scopes": [DB_QUERY]},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["scopes"] == [DB_QUERY]
    assert body["status"] == "ACTIVE"
    # manifest が承認の正本として渡る(二重閉包の内側)。
    assert captured["permissions"] == sorted([DB_QUERY, RAG_SEARCH])
    assert captured["approved_by"] == "dev-user"
    # 監査: 誰が/どの scope を APPROVE。
    assert audit and audit[0]["decision"] == "APPROVE"
    assert audit[0]["scope"] == DB_QUERY
    assert "dev-user" in audit[0]["resource"]


def test_approve_out_of_manifest_scope_is_rejected_fail_closed(install, audit, monkeypatch):
    # approve_scopes が manifest 非宣言スコープを GrantError に倒す。ルートは 422 + DENY 監査。
    def fake_approve(manifest, *, tenant, scopes, approved_by):
        raise pg.GrantError("manifest が要求していないスコープは承認できない")

    monkeypatch.setattr(pg, "approve_scopes", fake_approve)
    res = _client(monkeypatch).post(
        "/platform/grants",
        json={"tenant": TENANT, "plugin_id": PLUGIN, "scopes": [CONNECTOR_INVOKE]},
    )
    assert res.status_code == 422, res.text
    assert audit and audit[0]["decision"] == "DENY"
    assert audit[0]["scope"] == CONNECTOR_INVOKE


def test_approve_requires_admin(install, monkeypatch):
    monkeypatch.setattr(pg, "approve_scopes", lambda *a, **k: pytest.fail("should not reach"))
    res = _client(monkeypatch, admin=False).post(
        "/platform/grants",
        json={"tenant": TENANT, "plugin_id": PLUGIN, "scopes": [DB_QUERY]},
    )
    assert res.status_code == 403, res.text


def test_approve_unknown_plugin_is_404(audit, monkeypatch):
    monkeypatch.setattr(plugin_store, "list_installs", lambda plugin_id=None: [])
    monkeypatch.setattr(plugin_store, "find_install", lambda pid, ver: None)
    res = _client(monkeypatch).post(
        "/platform/grants",
        json={"tenant": TENANT, "plugin_id": "ghost/none", "scopes": [DB_QUERY]},
    )
    assert res.status_code == 404, res.text


def test_approve_latest_manifest_broken_is_422_no_fallback(monkeypatch):
    # 最新版が壊れている(manifest_error)。古い正常版へ降りず 422(fail-closed / review F-003)。
    monkeypatch.setattr(
        plugin_store, "list_installs",
        lambda plugin_id=None: [
            {"plugin_id": PLUGIN, "version": "2.0.0", "manifest": None, "manifest_error": True},
            {"plugin_id": PLUGIN, "version": "1.2.0",
             "manifest": _manifest_dict(), "manifest_error": False},
        ],
    )
    monkeypatch.setattr(pg, "approve_scopes", lambda *a, **k: pytest.fail("must not reach"))
    res = _client(monkeypatch).post(
        "/platform/grants",
        json={"tenant": TENANT, "plugin_id": PLUGIN, "scopes": [DB_QUERY]},
    )
    assert res.status_code == 422, res.text


def test_candidates_uses_latest_version_only(monkeypatch):
    # 最新版が壊れている → 古い版の宣言スコープへ降りず、候補から外す(review F-003)。
    monkeypatch.setattr(
        plugin_store, "list_installs",
        lambda plugin_id=None: [
            {"plugin_id": PLUGIN, "version": "2.0.0", "manifest": None, "manifest_error": True},
            {"plugin_id": PLUGIN, "version": "1.2.0",
             "manifest": _manifest_dict(), "manifest_error": False},
        ],
    )
    res = _client(monkeypatch).get("/platform/grants/candidates")
    assert res.status_code == 200, res.text
    assert res.json()["candidates"] == []


def test_approve_with_explicit_version_uses_find_install(install, audit, monkeypatch):
    # 実 UI は候補の version を常に送る。version 指定で find_install 経路(版固定)を通す。
    captured = {}

    def fake_approve(manifest, *, tenant, scopes, approved_by):
        captured["version"] = manifest.version
        return {
            "id": "g1", "tenant": tenant, "plugin_id": PLUGIN, "source_version": "1.2.0",
            "scopes": sorted(scopes), "status": "ACTIVE", "approved_by": approved_by,
            "created_at": "t", "updated_at": "t",
        }

    monkeypatch.setattr(pg, "approve_scopes", fake_approve)
    res = _client(monkeypatch).post(
        "/platform/grants",
        json={"tenant": TENANT, "plugin_id": PLUGIN, "version": "1.2.0", "scopes": [DB_QUERY]},
    )
    assert res.status_code == 200, res.text
    assert captured["version"] == "1.2.0"


def test_approve_too_many_scopes_is_422_before_audit(install, audit, monkeypatch):
    # 件数上限(語彙サイズ)を超える scopes は監査・検証前に 422(無制限な監査書込の予防)。
    monkeypatch.setattr(pg, "approve_scopes", lambda *a, **k: pytest.fail("must not reach"))
    too_many = [f"platform:x{i}" for i in range(20)]
    res = _client(monkeypatch).post(
        "/platform/grants",
        json={"tenant": TENANT, "plugin_id": PLUGIN, "scopes": too_many},
    )
    assert res.status_code == 422, res.text
    assert audit == []  # 監査も走らない


def test_candidates_skips_corrupt_non_dict_manifest(monkeypatch):
    # 壊れた CLOB が list/str で返っても md.get で 500 にせず候補外にする(fail-closed)。
    monkeypatch.setattr(
        plugin_store, "list_installs",
        lambda plugin_id=None: [
            {"plugin_id": "bad/one", "version": "1.0.0", "manifest": ["not", "a", "dict"],
             "manifest_error": False},
        ],
    )
    res = _client(monkeypatch).get("/platform/grants/candidates")
    assert res.status_code == 200, res.text
    assert res.json()["candidates"] == []


def test_approve_empty_tenant_is_422(install, monkeypatch):
    monkeypatch.setattr(pg, "approve_scopes", lambda *a, **k: pytest.fail("should not reach"))
    res = _client(monkeypatch).post(
        "/platform/grants",
        json={"tenant": "   ", "plugin_id": PLUGIN, "scopes": [DB_QUERY]},
    )
    assert res.status_code == 422, res.text


# --- revoke(失効 / 監査) ----------------------------------------------------


def test_revoke_active_grant_audits_actual_revoked_scopes(audit, monkeypatch):
    # 原子的失効。監査に載る scope は revoke_grant_capture が返した実失効 scope と一致する。
    monkeypatch.setattr(
        pg, "revoke_grant_capture", lambda tenant, pid: [DB_QUERY, RAG_SEARCH]
    )
    res = _client(monkeypatch).request(
        "DELETE", "/platform/grants",
        json={"tenant": TENANT, "plugin_id": PLUGIN},
    )
    assert res.status_code == 200, res.text
    assert res.json()["revoked"] is True
    decisions = {a["decision"] for a in audit}
    assert decisions == {"REVOKE"}
    assert {a["scope"] for a in audit} == {DB_QUERY, RAG_SEARCH}


def test_revoke_missing_grant_is_404(audit, monkeypatch):
    monkeypatch.setattr(pg, "revoke_grant_capture", lambda tenant, pid: None)
    res = _client(monkeypatch).request(
        "DELETE", "/platform/grants",
        json={"tenant": TENANT, "plugin_id": PLUGIN},
    )
    assert res.status_code == 404, res.text


# --- list / candidates ------------------------------------------------------


def test_list_grants_filters_pass_through(monkeypatch):
    seen = {}

    def fake_list(tenant, plugin_id, *, status):
        seen.update(tenant=tenant, plugin_id=plugin_id, status=status)
        return [{"id": "g1", "tenant": tenant, "plugin_id": PLUGIN, "scopes": [DB_QUERY]}]

    monkeypatch.setattr(pg, "list_grants", fake_list)
    res = _client(monkeypatch).get(f"/platform/grants?tenant={TENANT}&status=ACTIVE")
    assert res.status_code == 200, res.text
    assert res.json()["grants"][0]["plugin_id"] == PLUGIN
    assert seen == {"tenant": TENANT, "plugin_id": None, "status": "ACTIVE"}


def test_candidates_lists_installed_with_platform_scopes(install, monkeypatch):
    res = _client(monkeypatch).get("/platform/grants/candidates")
    assert res.status_code == 200, res.text
    cands = res.json()["candidates"]
    assert len(cands) == 1
    assert cands[0]["plugin_id"] == PLUGIN
    assert cands[0]["declared_scopes"] == sorted([DB_QUERY, RAG_SEARCH])


def test_candidates_skips_plugins_without_platform_scopes(monkeypatch):
    monkeypatch.setattr(
        plugin_store, "list_installs",
        lambda plugin_id=None: [
            {"plugin_id": "x/y", "version": "1.0.0",
             "manifest": _manifest_dict(permissions=[]), "manifest_error": False}
        ],
    )
    res = _client(monkeypatch).get("/platform/grants/candidates")
    assert res.status_code == 200, res.text
    assert res.json()["candidates"] == []


def test_grants_endpoints_require_admin(monkeypatch):
    monkeypatch.setattr(pg, "list_grants", lambda *a, **k: [])
    res = _client(monkeypatch, admin=False).get("/platform/grants")
    assert res.status_code == 403, res.text
