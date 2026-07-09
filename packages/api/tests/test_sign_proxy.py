"""署名プロキシ後継(jetuse_core.sign_proxy — SP3-06)の契約テスト。

SP3-03 spike の負契約(完全一致 allowlist・本文上限・パーサ差分拒否)を引き継ぎ、
SP3-06 のルーティング表(モデル → エンドポイント/auth プロファイル/compartment/api 種別)を検査。
上流 OCI へは出ない(httpx クライアントをフェイクに差し替えて build_request を捕捉)。
"""

import json

import pytest
from fastapi.testclient import TestClient

from jetuse_core import sign_proxy as sp
from jetuse_core.settings import get_settings

SELF_COMP = "ocid1.compartment.oc1..selftest"
SHARED_COMP = "ocid1.compartment.oc1..sharedtest"


@pytest.fixture
def env(monkeypatch):
    """環境依存値を固定(実 .env に依存しない)。"""
    monkeypatch.setenv("COMPARTMENT_OCID", SELF_COMP)
    monkeypatch.setenv("GEN_SHARED_PROFILE", "TESTSHARED")
    monkeypatch.setenv("GEN_SHARED_COMPARTMENT_OCID", SHARED_COMP)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def upstream(monkeypatch):
    """上流をフェイク化し、(profile, url, headers, content) を捕捉する。"""
    calls = []

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}

        async def aiter_bytes(self):
            yield b'{"ok":true}'

        async def aclose(self):
            return None

    class _Client:
        def __init__(self, profile):
            self.profile = profile

        def build_request(self, method, url, headers=None, content=None):
            calls.append({"profile": self.profile, "method": method, "url": url,
                          "headers": dict(headers or {}), "content": content})
            return object()

        async def send(self, req, stream=True):
            return _Resp()

    monkeypatch.setattr(sp, "_client", lambda profile: _Client(profile))
    return calls


@pytest.fixture
def client(env, upstream):
    return TestClient(sp.app)


def _post(client, path, body, **kw):
    return client.post(f"/v1/{path}", content=body,
                       headers={"content-type": "application/json"}, **kw)


# --- 負契約: 2 パス完全一致以外・メソッド・Content-Type・本文(SP3-03 の防御維持) ---

def test_path_allowlist_is_exact_two(client, upstream):
    # %2F 等のエンコードはフレームワークがデコードした後に完全一致判定へ入るため、
    # どんな生表現でも上流パスは許可 2 定数のどちらかにしかならない(迂回不能)。
    for path in ("files", "conversations", "vector_stores", "chat/completions/x",
                 "chat/../responses", "responses/x", ""):
        res = _post(client, path, b'{"model":"openai.gpt-oss-120b"}')
        assert res.status_code == 403, path
    assert upstream == []  # 上流へ一切出ない


def test_method_and_content_type(client, upstream):
    assert client.get("/v1/chat/completions").status_code == 405
    res = client.post("/v1/chat/completions", content=b"x=1",
                      headers={"content-type": "text/plain"})
    assert res.status_code == 415
    assert upstream == []


def test_unknown_model_403(client, upstream):
    res = _post(client, "chat/completions", b'{"model":"openai.gpt-4o"}')
    assert res.status_code == 403
    assert res.json()["error"] == "model_not_allowed"
    assert upstream == []


def test_invalid_body_400(client, upstream):
    for body in (b"not-json", b"[]", b"\xff\xfe",
                 b'{"model":"openai.gpt-oss-120b","model":"evil"}'):  # 重複キー(パーサ差分)
        assert _post(client, "chat/completions", body).status_code == 400, body
    assert _post(client, "chat/completions", b'{"model":[]}').status_code == 403
    assert _post(client, "chat/completions", b"{}").status_code == 403
    assert upstream == []


def test_body_too_large_413(client, upstream):
    big = b'{"model":"openai.gpt-oss-120b","x":"' + b"a" * sp._MAX_BODY + b'"}'
    assert _post(client, "chat/completions", big).status_code == 413
    assert upstream == []


# --- ルーティング表: api 種別 × テナンシ × リージョン(SP3-06) ---

def test_api_mismatch_both_directions_403(client, upstream):
    # gpt-5 系(responses)を chat/completions へ流せない
    res = _post(client, "chat/completions", b'{"model":"openai.gpt-5.1-codex-mini"}')
    assert (res.status_code, res.json()["error"]) == (403, "model_api_mismatch")
    res = _post(client, "chat/completions", b'{"model":"openai.gpt-5.6-sol"}')
    assert (res.status_code, res.json()["error"]) == (403, "model_api_mismatch")
    # chat 系(120b)を responses へも流せない
    res = _post(client, "responses", b'{"model":"openai.gpt-oss-120b"}')
    assert (res.status_code, res.json()["error"]) == (403, "model_api_mismatch")
    assert upstream == []


def test_default_model_routes_to_self_tenancy_chat(client, upstream):
    res = _post(client, "chat/completions", b'{"model":"openai.gpt-oss-120b"}')
    assert res.status_code == 200
    (call,) = upstream
    assert call["url"] == ("https://inference.generativeai.ap-osaka-1.oci.oraclecloud.com"
                           "/openai/v1/chat/completions")
    assert call["profile"] == ""  # 既定 auth(自テナンシ)
    assert call["headers"]["CompartmentId"] == SELF_COMP
    assert call["headers"]["opc-compartment-id"] == SELF_COMP


def test_gpt56_family_routes_to_shared_chicago_responses(client, upstream):
    # gpt-5.6 系は Chicago 限定 + responses(function tools が chat では不可 — E2E 実測)
    res = _post(client, "responses", b'{"model":"openai.gpt-5.6-sol"}')
    assert res.status_code == 200
    (call,) = upstream
    assert call["url"] == ("https://inference.generativeai.us-chicago-1.oci.oraclecloud.com"
                           "/openai/v1/responses")
    assert call["profile"] == "TESTSHARED"
    assert call["headers"]["CompartmentId"] == SHARED_COMP


def test_responses_family_routes_to_shared_osaka(client, upstream):
    res = _post(client, "responses", b'{"model":"openai.gpt-5.1-codex-mini"}')
    assert res.status_code == 200
    (call,) = upstream
    assert call["url"] == ("https://inference.generativeai.ap-osaka-1.oci.oraclecloud.com"
                           "/openai/v1/responses")
    assert call["profile"] == "TESTSHARED"
    assert call["headers"]["CompartmentId"] == SHARED_COMP


def test_shared_unconfigured_is_fail_closed(env, upstream, monkeypatch):
    monkeypatch.setenv("GEN_SHARED_PROFILE", "")
    get_settings.cache_clear()
    client = TestClient(sp.app)
    res = _post(client, "responses", b'{"model":"openai.gpt-5.1-codex-mini"}')
    assert (res.status_code, res.json()["error"]) == (403, "model_not_configured")
    assert upstream == []


# --- SP3-09: 共有テナンシの Vault 経路(デプロイ環境 — GEN_SHARED_SECRET_OCID) ---

def test_vault_route_takes_precedence_over_profile(env, upstream, monkeypatch):
    # secret OCID 設定時は profile(TESTSHARED)より Vault を優先(デプロイの正経路)
    monkeypatch.setenv("GEN_SHARED_SECRET_OCID", "ocid1.vaultsecret.oc1..t")
    get_settings.cache_clear()
    monkeypatch.setattr(sp.gen_shared_vault, "get_auth", lambda: object())
    client = TestClient(sp.app)
    res = _post(client, "responses", b'{"model":"openai.gpt-5.6-sol"}')
    assert res.status_code == 200
    (call,) = upstream
    assert call["profile"] == sp._VAULT
    assert call["headers"]["CompartmentId"] == SHARED_COMP


def test_vault_fetch_failure_fails_closed_without_profile_fallback(env, upstream, monkeypatch):
    # Vault 取得失敗は 403。profile が設定されていても fallback しない(経路を混ぜない)
    monkeypatch.setenv("GEN_SHARED_SECRET_OCID", "ocid1.vaultsecret.oc1..t")
    get_settings.cache_clear()
    monkeypatch.setattr(sp.gen_shared_vault, "get_auth", lambda: None)
    client = TestClient(sp.app)
    res = _post(client, "responses", b'{"model":"openai.gpt-5.6-sol"}')
    assert (res.status_code, res.json()["error"]) == (403, "model_not_configured")
    assert upstream == []


def test_auth_for_vault_key_returns_vault_auth(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(sp.gen_shared_vault, "get_auth", lambda: sentinel)
    assert sp._auth_for(sp._VAULT) is sentinel


def test_slow_vault_fetch_does_not_block_event_loop(env, upstream, monkeypatch):
    """Vault フェッチ(同期 SDK I/O)中もイベントループは回り続ける(review-1 M001)。

    フェッチに 0.3s かかる間、同一ループ上の他タスク(10ms tick)が進むことを観測する。
    proxy が get_auth を同期呼び出しすると tick はほぼ進まない(退行検知)。
    """
    import asyncio
    import time as _time

    monkeypatch.setenv("GEN_SHARED_SECRET_OCID", "ocid1.vaultsecret.oc1..t")
    get_settings.cache_clear()

    def slow_get_auth():
        _time.sleep(0.3)
        return object()
    monkeypatch.setattr(sp.gen_shared_vault, "get_auth", slow_get_auth)

    import httpx as _httpx

    async def main():
        transport = _httpx.ASGITransport(app=sp.app)
        async with _httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            req = asyncio.ensure_future(c.post(
                "/v1/responses", content=b'{"model":"openai.gpt-5.6-sol"}',
                headers={"content-type": "application/json"}))
            ticks = 0
            while not req.done() and ticks < 200:
                await asyncio.sleep(0.01)
                ticks += 1
            return (await req).status_code, ticks

    status, ticks = asyncio.run(main())
    assert status == 200
    assert ticks >= 10  # 同期ブロックだとループが止まり tick はほぼ 0


def test_self_tenancy_chat_keeps_max_tokens(client, upstream):
    # 自テナンシ(120b)は従来どおり max_tokens を受ける — 挙動不変(回帰)
    res = _post(client, "chat/completions",
                b'{"model":"openai.gpt-oss-120b","max_tokens":100}')
    assert res.status_code == 200
    body = json.loads(upstream[0]["content"])
    assert body["max_tokens"] == 100 and "max_completion_tokens" not in body


def test_responses_forces_store_false(client, upstream):
    # review-2 B001: 共有テナンシへ永続状態を作らせない(上流は ZDR で実効 store=false —
    # canonical でも明示 false に固定して契約を見える化)
    res = _post(client, "responses", b'{"model":"openai.gpt-5.1-codex-mini","input":"x"}')
    assert res.status_code == 200
    body = json.loads(upstream[0]["content"])
    assert body["store"] is False


def test_responses_strips_server_stored_item_references(client, upstream):
    # ZDR 上流は過去 turn の item を id 解決できない(rs_ not found で非決定失敗 — E2E 実測)。
    # @ai-sdk/openai が echo する item_reference(と bare reasoning)は落とし、
    # 内容完結な function call/output・message は残す
    body_in = (b'{"model":"openai.gpt-5.1-codex-mini","input":['
               b'{"type":"item_reference","id":"rs_x"},'
               b'{"type":"reasoning","id":"rs_y"},'
               b'{"type":"function_call","call_id":"c1","name":"bash","arguments":"{}"},'
               b'{"type":"function_call_output","call_id":"c1","output":"ok"},'
               b'{"role":"user","content":"next"}]}')
    res = _post(client, "responses", body_in)
    assert res.status_code == 200
    body = json.loads(upstream[0]["content"])
    assert [it.get("type", "message") for it in body["input"]] == [
        "function_call", "function_call_output", "message"]


def test_responses_rejects_persistence_params(client, upstream):
    # 明示 store:true / conversation / previous_response_id は 403(fail-closed)
    for body in (b'{"model":"openai.gpt-5.1-codex-mini","store":true}',
                 b'{"model":"openai.gpt-5.1-codex-mini","conversation":"conv_x"}',
                 b'{"model":"openai.gpt-5.1-codex-mini","previous_response_id":"resp_x"}'):
        res = _post(client, "responses", body)
        assert (res.status_code, res.json()["error"]) == (403, "persistence_not_allowed"), body
    assert upstream == []


def test_chat_body_has_no_store_injected(client, upstream):
    # chat/completions に store は存在しない — 注入しない(挙動不変)
    res = _post(client, "chat/completions", b'{"model":"openai.gpt-oss-120b"}')
    assert res.status_code == 200
    assert "store" not in json.loads(upstream[0]["content"])


def test_upstream_errors_map_to_502_504(env, monkeypatch):
    # review-2 m001: 上流未到達/タイムアウトを汎用 500 にしない
    import httpx as _httpx

    class _Client:
        def build_request(self, *a, **kw):
            return object()

        async def send(self, req, stream=True):
            raise self.exc

    c = _Client()
    monkeypatch.setattr(sp, "_client", lambda profile: c)
    tc = TestClient(sp.app, raise_server_exceptions=False)
    c.exc = _httpx.ReadTimeout("slow")
    res = _post(tc, "chat/completions", b'{"model":"openai.gpt-oss-120b"}')
    assert (res.status_code, res.json()["error"]) == (504, "upstream_timeout")
    c.exc = _httpx.ConnectError("down")
    res = _post(tc, "chat/completions", b'{"model":"openai.gpt-oss-120b"}')
    assert (res.status_code, res.json()["error"]) == (502, "upstream_unreachable")


def test_own_tenancy_auth_follows_auth_mode(monkeypatch):
    """配備(Container Instance)では AUTH_MODE=resource_principal で RP 署名(コンテナに
    ~/.oci の DEFAULT プロファイルを置かない — SP3-07)。共有テナンシは常にユーザープリンシパル。"""
    from jetuse_core import genai

    class _RP:
        pass

    class _UP:
        def __init__(self, profile_name=None):
            self.profile_name = profile_name

    monkeypatch.setattr(genai, "OciResourcePrincipalAuth", _RP)
    monkeypatch.setattr(genai, "OciUserPrincipalAuth", _UP)
    monkeypatch.setattr(sp, "OciUserPrincipalAuth", _UP)
    monkeypatch.setenv("AUTH_MODE", "resource_principal")
    assert isinstance(sp._auth_for(""), _RP)
    monkeypatch.delenv("AUTH_MODE")
    assert isinstance(sp._auth_for(""), _UP)
    assert sp._auth_for("SHAREDPROF").profile_name == "SHAREDPROF"


def test_client_headers_are_not_forwarded(client, upstream):
    res = client.post(
        "/v1/chat/completions", content=b'{"model":"openai.gpt-oss-120b"}',
        headers={"content-type": "application/json", "accept": "text/event-stream",
                 "authorization": "Bearer dummy", "CompartmentId": "ocid1.evil",
                 "opc-compartment-id": "ocid1.evil"})
    assert res.status_code == 200
    (call,) = upstream
    h = {k.lower(): v for k, v in call["headers"].items()}
    assert "authorization" not in h                     # 署名系はクライアントから受けない
    assert h["compartmentid"] == SELF_COMP              # サーバ側で固定(上書き不可)
    assert h["opc-compartment-id"] == SELF_COMP
    assert h["accept"] == "text/event-stream"           # 通信メタは通す(SSE)
    # 正規再シリアライズ本文を転送(生 body でない)
    assert call["content"] == b'{"model": "openai.gpt-oss-120b"}'
