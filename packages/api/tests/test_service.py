import pytest
from fastapi.testclient import TestClient

from jetuse_core.settings import get_settings
from service.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_healthz():
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_api_health():
    # gateway は /api/* しか CI へルートしないため、/api 配下の health が smoke の契約(SP3-07)
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_gen_proxy_mounted_with_allowlist():
    # sign_proxy は API プロセス内 mount(SP3-07 配備像)。未知モデル 403 = allowlist が生きている証拠
    res = client.post(
        "/gen-proxy/v1/chat/completions",
        content=b'{"model": "not-allowed"}',
        headers={"content-type": "application/json"})
    assert res.status_code == 403
    assert res.json()["error"] == "model_not_allowed"


def test_sse_ping_streams_events_with_keepalive():
    res = client.get("/api/chat/ping", params={"events": 3, "delay": 0})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    body = res.text
    assert body.startswith('data: {"ka": 1}')  # keepaliveはdataフレーム(2026-06-11変更)
    assert body.count("data: ") == 5  # keepalive + 3イベント + [DONE]
    assert body.rstrip().endswith("data: [DONE]")
    assert '"user": "dev-user"' in body  # AUTH_REQUIRED=false の暫定ユーザー


def test_auth_required_rejects_missing_token(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    get_settings.cache_clear()
    res = client.get("/api/chat/ping")
    assert res.status_code == 401


def test_auth_required_fails_closed_without_oidc_config(monkeypatch):
    # OIDC未設定のままトークンを出されても素通りさせない(fail-closed)
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    get_settings.cache_clear()
    res = client.get("/api/chat/ping", headers={"Authorization": "Bearer dummy"})
    assert res.status_code == 500
