"""ARCH-02: fnルーターのルーティング/バリデーション単体テスト(DB・OCIはモック)"""

import json

import pytest

from fn.router import func as router


class FakeCtx:
    def __init__(self, method: str, path: str, auth: str | None = "x"):
        self._headers = {
            "fn-http-method": method,
            "fn-http-request-url": path,
        }
        if auth:
            self._headers["fn-http-h-authorization"] = f"Bearer {auth}"

    def Headers(self):  # noqa: N802 (FDK互換)
        return self._headers

    # fdk.response.Response が参照する属性
    def GetResponseHeaders(self):  # noqa: N802
        return {}

    def SetResponseHeaders(self, headers, status_code):  # noqa: N802
        self.response_headers = headers
        self.status = status_code


def call(method, path, body=None, auth="x"):
    import io

    ctx = FakeCtx(method, path, auth)
    resp = router.handler(ctx, io.BytesIO(json.dumps(body or {}).encode()))
    return ctx.status, json.loads(resp.body()) if resp.body() else None


@pytest.fixture(autouse=True)
def no_auth(monkeypatch):
    # AUTH_REQUIRED=false相当(verify_tokenがdev-userを返す)
    from jetuse_core.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    yield
    get_settings.cache_clear()


def test_unknown_route_404():
    status, body = call("GET", "/api/unknown")
    assert status == 404
    assert "no route" in body["detail"]


def test_presets_validation():
    status, body = call("POST", "/api/presets", {"name": "", "content": ""})
    assert status == 422


def test_presets_list(monkeypatch):
    monkeypatch.setattr(router.preset_repo, "list_presets", lambda owner: [{"id": "1"}])
    status, body = call("GET", "/api/presets")
    assert status == 200
    assert body == {"presets": [{"id": "1"}]}


def test_dbchat_execute_validation():
    status, body = call("POST", "/api/dbchat/execute", {"sql": ""})
    assert status == 422


def test_dbchat_select_ai_models():
    # feedback 20260620 #3: dbchatセグメントはFn経由のため、モデル一覧もFnルーターに必要
    status, body = call("GET", "/api/dbchat/select-ai-models")
    assert status == 200
    assert body["default"] == router.nl2sql.DEFAULT_SELECT_AI_MODEL
    assert any(m["key"] == router.nl2sql.DEFAULT_SELECT_AI_MODEL for m in body["models"])


def test_dbchat_execute_rejected(monkeypatch):
    def raise_rejected(sql, owner_key=None):  # Fn は owner_key を渡す(M003)
        raise router.nl2sql.SqlRejectedError("SELECTのみ")

    monkeypatch.setattr(router.nl2sql, "execute_readonly", raise_rejected)
    status, body = call("POST", "/api/dbchat/execute", {"sql": "DROP TABLE x"})
    assert status == 400


def test_dbchat_execute_owner_key_pending_returns_503(monkeypatch):
    """review-12 M002: execute_readonly が共有チョークポイントで送出する
    OwnerKeyPreflightError を Fn ルーターも 503 に正規化する(FastAPI と同契約。500 にしない)。"""
    def raise_pending(sql, owner_key=None):
        raise router.OwnerKeyPreflightError("pending")

    monkeypatch.setattr(router.nl2sql, "execute_readonly", raise_pending)
    status, body = call("POST", "/api/dbchat/execute", {"sql": "SELECT 1 FROM dual"})
    assert status == 503


def test_tts_validation():
    status, body = call("POST", "/api/tts", {"text": "a", "voice": "Nobody"})
    assert status == 422
    assert "unknown voice" in body["detail"]
