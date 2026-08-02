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
    # 空文字を明示する(delenv だと Settings が .env から拾い直し、OIDC を設定済みの開発機で落ちる)
    monkeypatch.setenv("OIDC_ISSUER", "")
    monkeypatch.setenv("OIDC_JWKS_URL", "")
    get_settings.cache_clear()
    res = client.get("/api/chat/ping", headers={"Authorization": "Bearer dummy"})
    assert res.status_code == 500


def test_max_tool_hops_rejects_boolean():
    """`true` が 1 として通らないこと(AGT-04 review-10)。

    bool は int の派生なので、素の int 宣言だと JSON の真偽値が黙って 1 になる。
    上限の指定に真偽値が来るのは誤りで、**API 境界で断る**のが
    `chat.resolve_max_tool_hops` の bool 拒否と揃った挙動。
    """
    res = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b",
        "messages": [{"role": "user", "content": "x"}],
        "agent": True, "max_tool_hops": True,
    })
    assert res.status_code == 422
