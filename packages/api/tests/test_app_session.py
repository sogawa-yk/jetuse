"""app-session Cookie ブートストラップの契約テスト(ADR-0023 §3.5 — SP3-03 単体担保)。

AUTH_REQUIRED=true で: Cookie なし=401 / 有効 Cookie=200 / 期限切れコード=401 /
app-session(Cookie)経由の owner mutation=403。実トークン・ブラウザ全経路 E2E は SP3-05。
"""

import time

import pytest
from fastapi.testclient import TestClient

import jetuse_core.app_session as app_session
import service.demo_context as demo_context
import service.routes.demos as droute
from jetuse_core.settings import get_settings
from service.demo_context import APP_COOKIE
from service.main import app

client = TestClient(app)
BUNDLE = "12345678-1234-1234-1234-123456789abc"
DEMO = {"id": "d1", "owner_sub": "alice", "name": "n", "visibility": "private",
        "status": "ready", "config": {"frontend": {"bundle": BUNDLE}}}


@pytest.fixture
def auth_on(monkeypatch):
    """AUTH_REQUIRED=true + app_session 秘密鍵。ready デモ d1(所有者 alice)をモック。"""
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.setenv("APP_SESSION_SECRET", "unit-test-secret")
    get_settings.cache_clear()
    monkeypatch.setattr(demo_context.demos, "get_demo", lambda i: dict(DEMO))
    monkeypatch.setattr(droute.demos, "get_demo", lambda i: dict(DEMO))
    monkeypatch.setattr(droute.bundles, "get_object",
                        lambda ns, b, rel: b"<!doctype html>" if rel == "index.html" else None)
    yield
    get_settings.cache_clear()


def _session_cookie(demo_id="d1", subject="alice"):
    return app_session.issue_session(demo_id, subject)


def test_delivery_without_auth_is_401(auth_on):
    # Cookie/Bearer/コードのいずれも無い → 401(fail-closed)
    res = client.get("/api/demos/d1/app/", follow_redirects=False)
    assert res.status_code == 401


def test_delivery_with_valid_cookie_is_200(auth_on):
    res = client.get("/api/demos/d1/app/", cookies={APP_COOKIE: _session_cookie()})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")


def test_delivery_with_expired_code_is_401(auth_on):
    # 署名は正しいが exp が過去 = 期限切れコード → 401
    secret = b"unit-test-secret"
    expired = app_session._sign(
        {"t": "code", "d": "d1", "s": "alice", "exp": int(time.time()) - 10}, secret)
    res = client.get(f"/api/demos/d1/app/?c={expired}", follow_redirects=False)
    assert res.status_code == 401


def test_valid_code_sets_cookie_then_serves(auth_on):
    # 一回性コード → Cookie 発行 + ?c= 無しの同 URL へ 303(コードを URL/履歴に残さない)
    code = app_session.issue_code("d1", "alice")
    res = client.get(f"/api/demos/d1/app/?c={code}", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/api/demos/d1/app/"  # ?c= が落ちている
    sc = res.headers.get("set-cookie", "")
    assert APP_COOKIE in sc and "HttpOnly" in sc and "Secure" in sc
    assert "samesite=strict" in sc.lower().replace(" ", "")
    assert "Path=/api/demos/d1/" in sc
    # 発行された Cookie で以降の配信が 200
    m = __import__("re").search(rf"{APP_COOKIE}=([^;]+)", sc)
    res2 = client.get("/api/demos/d1/app/", cookies={APP_COOKIE: m.group(1)})
    assert res2.status_code == 200


def test_owner_mutation_via_cookie_is_403(auth_on):
    # Cookie(生成 SPA 面)からの owner mutation は 403(Bearer 親面のみ許可)
    res = client.post("/api/demos/d1/rag/files",
                      cookies={APP_COOKIE: _session_cookie()},
                      files={"file": ("a.txt", b"x", "text/plain")})
    assert res.status_code == 403


def test_capability_via_cookie_allowed(auth_on, monkeypatch):
    # 閲覧/実行(GET rag files)は Cookie で 200(owner mutation ではない = Cookie 受理)
    from starlette.responses import JSONResponse

    async def _list(ns):
        return JSONResponse({"files": []})
    monkeypatch.setattr(droute.rag_routes, "list_files_response", _list)
    res = client.get("/api/demos/d1/rag/files", cookies={APP_COOKIE: _session_cookie()})
    assert res.status_code == 200


def test_app_session_issue_requires_bearer_face(auth_on):
    # /app-session 発行は crud_router(Cookie 非対応)。Cookie だけでは発行できない = 401。
    res = client.post("/api/demos/d1/app-session", cookies={APP_COOKIE: _session_cookie()})
    assert res.status_code == 401
