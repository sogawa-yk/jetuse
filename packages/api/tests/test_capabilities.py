"""SP1-01: 能力カタログ(GET /api/capabilities)のテスト。

カタログの正しさの要 = ディスクリプタの routes が app.openapi() に実在すること
(ディスクリプタとルートの乖離をここで検出する)。
"""

import pytest
from fastapi.testclient import TestClient

from jetuse_core.capabilities import CAPABILITIES
from jetuse_core.settings import get_settings
from service.main import app

client = TestClient(app)

# specs/17 §4 の「デモ向け能力」8件
EXPECTED_CAPABILITIES = {
    "chat", "rag.search", "dbchat", "agents",
    "voice", "minutes", "translate", "docunderstand",
}


@pytest.fixture(autouse=True)
def reset_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_returns_eight_capabilities_matching_spec():
    res = client.get("/api/capabilities")
    assert res.status_code == 200
    caps = res.json()["capabilities"]
    assert len(caps) == 8
    assert {c["capability"] for c in caps} == EXPECTED_CAPABILITIES
    for c in caps:
        assert c["demo_safe"] is True
        assert c["summary"]
        assert c["when_to_use"]
        assert isinstance(c["example"], dict)
        assert c["routes"]


def test_descriptor_routes_exist_in_openapi():
    spec_paths = app.openapi()["paths"]
    for cap in CAPABILITIES:
        for route in cap["routes"]:
            assert route["path"] in spec_paths, (
                f"{cap['capability']}: {route['path']} not in openapi paths"
            )
            assert route["method"] in spec_paths[route["path"]], (
                f"{cap['capability']}: {route['method']} {route['path']}"
            )


def test_response_attaches_openapi_fragments():
    res = client.get("/api/capabilities")
    for c in res.json()["capabilities"]:
        for route in c["routes"]:
            frag = c["openapi"][route["path"]][route["method"]]
            assert "responses" in frag


def test_backstage_routes_not_in_catalog():
    # specs/17 §4: admin / conversations / tools / mcp_servers 等の裏方は載せない
    backstage = ("/api/admin", "/api/conversations", "/api/tools",
                 "/api/agent/mcp-servers", "/api/db/datasets", "/api/usecases")
    for cap in CAPABILITIES:
        for route in cap["routes"]:
            assert not route["path"].startswith(backstage), route["path"]


def test_requires_auth(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    get_settings.cache_clear()
    res = client.get("/api/capabilities")
    assert res.status_code == 401
