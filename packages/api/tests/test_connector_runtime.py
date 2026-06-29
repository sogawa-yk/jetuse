"""コネクタ実行(invoke)層の単体テスト(CON-02)。

正常系(builtin Slack 投稿 / mcp 配管)と、**fail-closed**(認可拒否で外部不到達・秘密未設定・
秘密が戻り値/監査に出ない・未知 action / payload 不正)を網羅する。実 Slack/実 MCP は投入せず、
http_caller / mcp_caller / secret_resolver を mock 注入して投稿フローを検証する。

ブローカー監査(`platform_broker_audit`)は best-effort で、DB 未接続のユニットでは握り潰される
(`record_broker_access` が例外をログのみに倒す)。許可/拒否が実 ADB の監査に残ることは E2E で見る。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from jetuse_core import platform_broker as pb
from jetuse_core.models import MODELS
from jetuse_core.platform_broker import issue_broker_token
from jetuse_core.plugins import connector_runtime
from jetuse_core.plugins.connector import validate_connector
from jetuse_core.plugins.connector_runtime import (
    ConnectorInvokeDenied,
    ConnectorInvokeError,
    InvokeRequest,
    invoke_connector_action,
)
from jetuse_core.plugins.slack_connector_builtin import (
    SLACK_POST_MESSAGE_URL,
    slack_connector_definition,
)
from jetuse_core.settings import Settings

TENANT = "ocid1.tenancy.oc1..aaaa-tenant-A"
TENANT_B = "ocid1.tenancy.oc1..bbbb-tenant-B"
PLUGIN = "jetuse/slack-connector"
FAKE_TOKEN = "xoxb-FAKE-NOT-A-REAL-TOKEN"


def _settings() -> Settings:
    return Settings(
        platform_broker_secret="unit-broker-secret-32bytes-minimum!!",
        platform_token_ttl_seconds=300,
    )


def _token(scopes, *, tenant=TENANT, **kw) -> str:
    return issue_broker_token(PLUGIN, tenant, scopes, settings=_settings(), **kw)


class _RecordingHttp:
    """http_caller の mock。呼び出しを記録し、Slack 風の応答を返す。"""

    def __init__(self, response=None):
        self.calls: list[tuple] = []
        self.response = response or {"ok": True, "channel": "C123", "ts": "1700000000.000100"}

    def __call__(self, url, headers, body):
        self.calls.append((url, headers, body))
        return self.response


def _resolver(ref: str) -> str:
    # 参照名 → 実トークン(mock)。実 Vault 束ねは CON-03。
    assert ref == "slack-bot-token"
    return FAKE_TOKEN


# --- 正常系: builtin Slack 投稿 -------------------------------------------


def test_invoke_slack_post_message_ok():
    http = _RecordingHttp()
    result = invoke_connector_action(
        slack_connector_definition(),
        "post_message",
        {"channel": "#general", "text": "こんにちは"},
        broker_token=_token(["platform:connector.invoke"]),
        tenant=TENANT,
        resource="unit-marker",
        settings=_settings(),
        secret_resolver=_resolver,
        http_caller=http,
    )
    assert result.ok is True
    assert result.provider == "slack"
    assert result.transport == "builtin"
    assert result.output["ts"] == "1700000000.000100"
    assert result.jti  # 監査突合用
    # http は1回・正しい URL・Bearer に解決トークン・body に channel/text。
    assert len(http.calls) == 1
    url, headers, body = http.calls[0]
    assert url == SLACK_POST_MESSAGE_URL
    assert headers["Authorization"] == f"Bearer {FAKE_TOKEN}"
    assert body == {"channel": "#general", "text": "こんにちは"}


def test_secret_never_leaks_into_result():
    http = _RecordingHttp()
    result = invoke_connector_action(
        slack_connector_definition(),
        "post_message",
        {"channel": "C1", "text": "hi"},
        broker_token=_token(["platform:connector.invoke"]),
        tenant=TENANT,
        settings=_settings(),
        secret_resolver=_resolver,
        http_caller=http,
    )
    # 実トークンは戻り値・output のどこにも現れない(Authorization ヘッダ内のみ)。
    assert FAKE_TOKEN not in json.dumps(result.output, ensure_ascii=False)
    assert FAKE_TOKEN not in repr(result)


def test_invoke_request_repr_hides_token():
    req = InvokeRequest(provider="slack", action="post_message", payload={}, token=FAKE_TOKEN)
    assert FAKE_TOKEN not in repr(req)


# --- fail-closed: 認可拒否で外部不到達 -------------------------------------


def test_denied_missing_invoke_scope_no_external_call():
    http = _RecordingHttp()
    with pytest.raises(ConnectorInvokeDenied):
        invoke_connector_action(
            slack_connector_definition(),
            "post_message",
            {"channel": "C1", "text": "hi"},
            # connector.invoke を付与しない → 拒否。
            broker_token=_token(["platform:rag.search"]),
            tenant=TENANT,
            settings=_settings(),
            secret_resolver=_resolver,
            http_caller=http,
        )
    assert http.calls == []  # 外部副作用ゼロ(Slack へ到達していない)


def test_denied_cross_tenant_no_external_call():
    http = _RecordingHttp()
    token = _token(["platform:connector.invoke"], tenant=TENANT)
    with pytest.raises(ConnectorInvokeDenied):
        invoke_connector_action(
            slack_connector_definition(),
            "post_message",
            {"channel": "C1", "text": "hi"},
            broker_token=token,
            tenant=TENANT_B,  # 別テナントへの越境
            settings=_settings(),
            secret_resolver=_resolver,
            http_caller=http,
        )
    assert http.calls == []


def test_denied_expired_token_no_external_call():
    http = _RecordingHttp()
    expired = _token(
        ["platform:connector.invoke"],
        ttl_seconds=60,
        now=datetime.now(UTC) - timedelta(hours=1),
    )
    with pytest.raises(ConnectorInvokeDenied):
        invoke_connector_action(
            slack_connector_definition(),
            "post_message",
            {"channel": "C1", "text": "hi"},
            broker_token=expired,
            tenant=TENANT,
            settings=_settings(),
            secret_resolver=_resolver,
            http_caller=http,
        )
    assert http.calls == []


def test_missing_broker_token_denied_and_audited(monkeypatch):
    # 空トークンは authorize を通らないが、DENY が監査に明示記録されること(CON02-MAJ-001)。
    recorded: list[dict] = []
    monkeypatch.setattr(
        pb, "record_broker_access", lambda **kw: recorded.append(kw)
    )
    http = _RecordingHttp()
    with pytest.raises(ConnectorInvokeDenied):
        invoke_connector_action(
            slack_connector_definition(),
            "post_message",
            {"channel": "C1", "text": "hi"},
            broker_token="  ",
            tenant=TENANT,
            resource="unit-marker",
            settings=_settings(),
            secret_resolver=_resolver,
            http_caller=http,
        )
    assert http.calls == []
    assert len(recorded) == 1
    assert recorded[0]["decision"] == "DENY"
    assert recorded[0]["reason"] == "missing_token"
    assert recorded[0]["scope"] == "platform:connector.invoke"
    assert recorded[0]["resource"] == "unit-marker"


# --- fail-closed: 秘密未設定 / 未知 action / payload --------------------------


def test_secret_resolver_required_for_oauth2():
    http = _RecordingHttp()
    with pytest.raises(ConnectorInvokeError):
        invoke_connector_action(
            slack_connector_definition(),
            "post_message",
            {"channel": "C1", "text": "hi"},
            broker_token=_token(["platform:connector.invoke"]),
            tenant=TENANT,
            settings=_settings(),
            secret_resolver=None,  # 解決器が無い → fail-closed
            http_caller=http,
        )
    assert http.calls == []


def test_secret_resolver_empty_value_rejected():
    http = _RecordingHttp()
    with pytest.raises(ConnectorInvokeError):
        invoke_connector_action(
            slack_connector_definition(),
            "post_message",
            {"channel": "C1", "text": "hi"},
            broker_token=_token(["platform:connector.invoke"]),
            tenant=TENANT,
            settings=_settings(),
            secret_resolver=lambda ref: "",
            http_caller=http,
        )
    assert http.calls == []


def test_unknown_action_rejected_before_authz():
    http = _RecordingHttp()
    with pytest.raises(ConnectorInvokeError):
        invoke_connector_action(
            slack_connector_definition(),
            "no_such_action",
            {},
            broker_token=_token(["platform:connector.invoke"]),
            tenant=TENANT,
            settings=_settings(),
            secret_resolver=_resolver,
            http_caller=http,
        )
    assert http.calls == []


def test_post_message_requires_channel_and_text():
    http = _RecordingHttp()
    with pytest.raises(ConnectorInvokeError):
        invoke_connector_action(
            slack_connector_definition(),
            "post_message",
            {"channel": "C1"},  # text 欠落
            broker_token=_token(["platform:connector.invoke"]),
            tenant=TENANT,
            settings=_settings(),
            secret_resolver=_resolver,
            http_caller=http,
        )
    assert http.calls == []


def test_slack_api_error_surfaces():
    http = _RecordingHttp(response={"ok": False, "error": "channel_not_found"})
    with pytest.raises(ConnectorInvokeError) as ei:
        invoke_connector_action(
            slack_connector_definition(),
            "post_message",
            {"channel": "C1", "text": "hi"},
            broker_token=_token(["platform:connector.invoke"]),
            tenant=TENANT,
            settings=_settings(),
            secret_resolver=_resolver,
            http_caller=http,
        )
    assert "channel_not_found" in str(ei.value)


# --- mcp transport 配管(mock) --------------------------------------------


def _mcp_def(auth=None):
    return validate_connector(
        {
            "provider": "teams",
            "transport": "mcp",
            "endpoint": "https://mcp.example.com/teams",
            "auth": auth or {"kind": "api_token", "secretRef": "teams-token"},
            "actions": [{"name": "send", "title": "送信"}],
        }
    )


def test_mcp_dispatch_builds_spec_with_bearer():
    captured = {}

    def mcp_caller(spec, action, payload):
        captured["spec"] = spec
        captured["action"] = action
        captured["payload"] = payload
        return {"ok": True, "mcp": True, "output_text": "done"}

    result = invoke_connector_action(
        _mcp_def(),
        "send",
        {"to": "team", "text": "hi"},
        broker_token=_token(["platform:connector.invoke"]),
        tenant=TENANT,
        settings=_settings(),
        secret_resolver=lambda ref: FAKE_TOKEN,
        mcp_caller=mcp_caller,
    )
    assert result.transport == "mcp"
    assert result.ok is True
    spec = captured["spec"]
    assert spec["type"] == "mcp"
    assert spec["server_label"] == "teams"
    assert spec["server_url"] == "https://mcp.example.com/teams"
    assert spec["require_approval"] == "never"
    assert spec["headers"]["Authorization"] == f"Bearer {FAKE_TOKEN}"
    assert captured["action"] == "send"
    # 実トークンは戻り値に漏れない。
    assert FAKE_TOKEN not in json.dumps(result.output, ensure_ascii=False)


def test_mcp_dispatch_no_headers_when_auth_none():
    captured = {}

    def mcp_caller(spec, action, payload):
        captured["spec"] = spec
        return {"ok": True}

    invoke_connector_action(
        _mcp_def(auth={"kind": "none"}),
        "send",
        {"x": 1},
        broker_token=_token(["platform:connector.invoke"]),
        tenant=TENANT,
        settings=_settings(),
        mcp_caller=mcp_caller,
    )
    assert "headers" not in captured["spec"]


# --- 既定 MCP caller のモデル解決(CON02-MAJ-002) ---------------------------


class _FakeResponses:
    def __init__(self, sink):
        self._sink = sink

    def create(self, **kw):
        self._sink.update(kw)
        return type("R", (), {"output_text": "ok"})()


class _FakeClient:
    def __init__(self, sink):
        self.responses = _FakeResponses(sink)


def test_default_mcp_caller_uses_responses_capable_model(monkeypatch):
    sink: dict = {}
    import jetuse_core.genai as genai

    monkeypatch.setattr(genai, "make_inference_client", lambda *a, **k: _FakeClient(sink))
    spec = {
        "type": "mcp",
        "server_label": "teams",
        "server_url": "https://x",
        "require_approval": "never",
    }
    out = connector_runtime._default_mcp_caller(spec, "send", {"a": 1})
    assert out["mcp"] is True
    # Responses 対応モデル(gpt-oss-120b)の oci_id が渡る(chat 専用 key ではない)。
    assert sink["model"] == MODELS[connector_runtime.MCP_DEFAULT_MODEL].oci_id
    assert MODELS[connector_runtime.MCP_DEFAULT_MODEL].api == "responses"
    assert sink["tools"] == [spec]
    assert sink["store"] is False


def test_resolve_responses_model_rejects_chat_model():
    # llama-3.3-70b は chat 専用 → mcp transport では fail-closed。
    with pytest.raises(ConnectorInvokeError):
        connector_runtime._resolve_responses_model("llama-3.3-70b")


def test_resolve_responses_model_rejects_unknown():
    with pytest.raises(ConnectorInvokeError):
        connector_runtime._resolve_responses_model("no-such-model")


# --- action.permissions の強制(CON02 review-2 MAJ) -------------------------


def _mcp_def_with_perms():
    return validate_connector(
        {
            "provider": "teams",
            "transport": "mcp",
            "endpoint": "https://mcp.example.com/teams",
            "auth": {"kind": "none"},
            "actions": [
                {
                    "name": "read_msgs",
                    "title": "メッセージ読取",
                    "permissions": ["platform:conversations.read"],
                }
            ],
        }
    )


def test_action_permissions_denied_without_extra_scope():
    calls = {"n": 0}

    def mcp_caller(spec, action, payload):
        calls["n"] += 1
        return {"ok": True}

    with pytest.raises(ConnectorInvokeDenied):
        invoke_connector_action(
            _mcp_def_with_perms(),
            "read_msgs",
            {},
            # connector.invoke はあるが action 宣言の conversations.read が無い → 拒否。
            broker_token=_token(["platform:connector.invoke"]),
            tenant=TENANT,
            settings=_settings(),
            mcp_caller=mcp_caller,
        )
    assert calls["n"] == 0  # transport 不到達


def test_action_permissions_allowed_with_both_scopes():
    calls = {"n": 0}

    def mcp_caller(spec, action, payload):
        calls["n"] += 1
        return {"ok": True}

    result = invoke_connector_action(
        _mcp_def_with_perms(),
        "read_msgs",
        {},
        broker_token=_token(["platform:connector.invoke", "platform:conversations.read"]),
        tenant=TENANT,
        settings=_settings(),
        mcp_caller=mcp_caller,
    )
    assert result.ok is True
    assert calls["n"] == 1


# --- secret 非漏洩の最終強制: echo / 例外からも漏らさない(CON02 review-2 MAJ) ---


def test_transport_echoing_token_is_redacted():
    def echo_caller(spec, action, payload):
        # transport が Authorization ヘッダ(トークン)を output に echo する悪い caller。
        return {"ok": True, "echo": spec["headers"]["Authorization"]}

    result = invoke_connector_action(
        _mcp_def(),
        "send",
        {"x": 1},
        broker_token=_token(["platform:connector.invoke"]),
        tenant=TENANT,
        settings=_settings(),
        secret_resolver=lambda ref: FAKE_TOKEN,
        mcp_caller=echo_caller,
    )
    dumped = json.dumps(result.output, ensure_ascii=False)
    assert FAKE_TOKEN not in dumped
    assert "***redacted***" in dumped


def test_transport_echoing_token_in_dict_key_is_redacted():
    def echo_key_caller(spec, action, payload):
        # トークンが dict の **キー** に混入するケース(値だけ redact では漏れる)。
        return {"ok": True, spec["headers"]["Authorization"]: "x"}

    result = invoke_connector_action(
        _mcp_def(),
        "send",
        {"x": 1},
        broker_token=_token(["platform:connector.invoke"]),
        tenant=TENANT,
        settings=_settings(),
        secret_resolver=lambda ref: FAKE_TOKEN,
        mcp_caller=echo_key_caller,
    )
    assert FAKE_TOKEN not in json.dumps(result.output, ensure_ascii=False)


def test_transport_exception_token_redacted_and_chain_broken():
    def boom_caller(spec, action, payload):
        raise RuntimeError(f"leaked {spec['headers']['Authorization']}")

    with pytest.raises(ConnectorInvokeError) as ei:
        invoke_connector_action(
            _mcp_def(),
            "send",
            {"x": 1},
            broker_token=_token(["platform:connector.invoke"]),
            tenant=TENANT,
            settings=_settings(),
            secret_resolver=lambda ref: FAKE_TOKEN,
            mcp_caller=boom_caller,
        )
    assert FAKE_TOKEN not in str(ei.value)
    # 例外連鎖(__cause__/__context__)経由でも漏れない。
    assert ei.value.__cause__ is None
    assert FAKE_TOKEN not in repr(ei.value.__context__)


def test_known_error_with_token_in_message_is_redacted_and_chain_cleared():
    # ハンドラが ConnectorInvokeError を投げ、そのメッセージにトークンが混入したケース。
    def caller(spec, action, payload):
        raise ConnectorInvokeError(f"slack rejected token {spec['headers']['Authorization']}")

    with pytest.raises(ConnectorInvokeError) as ei:
        invoke_connector_action(
            _mcp_def(),
            "send",
            {"x": 1},
            broker_token=_token(["platform:connector.invoke"]),
            tenant=TENANT,
            settings=_settings(),
            secret_resolver=lambda ref: FAKE_TOKEN,
            mcp_caller=caller,
        )
    assert FAKE_TOKEN not in str(ei.value)
    assert ei.value.__cause__ is None
    assert ei.value.__context__ is None


# --- BE-03: 実 HTTP caller(live_http_caller) -----------------------------


# live_http_caller は応答を**ストリーム読み**して絶対 wall-clock 期限を強制する(MAJ-002)ため、
# テストは httpx.Client().stream(...) のコンテキストマネージャ・プロトコルを模す fake で差し替える。
class _FakeStreamResp:
    """httpx の streaming response を模す(status_code ＋ iter_bytes)。"""

    def __init__(self, payload, *, status=200, json_ok=True, chunks=None, raise_on_iter=None):
        self.status_code = status
        if chunks is not None:
            self._chunks = chunks
        elif not json_ok:
            self._chunks = [b"<<not json>>"]
        else:
            self._chunks = [json.dumps(payload).encode()]
        self._raise_on_iter = raise_on_iter

    def iter_raw(self):
        if self._raise_on_iter is not None:
            raise self._raise_on_iter
        yield from self._chunks


class _FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self._resp

    def __exit__(self, *a):
        return False


class _FakeHttpClient:
    """httpx.Client を模す。`stream("POST", url, headers=, json=)` で要求を捕捉する。"""

    def __init__(self, *, resp=None, captured=None, stream_exc=None):
        self._resp = resp
        self._captured = captured
        self._stream_exc = stream_exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def stream(self, method, url, *, headers=None, json=None):
        if self._captured is not None:
            self._captured.update(method=method, url=url, headers=headers, json=json)
        if self._stream_exc is not None:
            raise self._stream_exc
        return _FakeStreamCtx(self._resp)


def _patch_client(monkeypatch, **kwargs):
    import httpx

    monkeypatch.setattr(httpx, "Client", lambda **kw: _FakeHttpClient(**kwargs))


def test_live_http_caller_posts_json_and_returns_dict(monkeypatch):
    captured: dict = {}
    _patch_client(
        monkeypatch,
        resp=_FakeStreamResp({"ok": True, "channel": "C1", "ts": "1.2"}),
        captured=captured,
    )
    out = connector_runtime.live_http_caller(
        "https://slack.com/api/chat.postMessage",
        {"Authorization": "Bearer xoxb-FAKE"},
        {"channel": "C1", "text": "hi"},
    )
    assert out == {"ok": True, "channel": "C1", "ts": "1.2"}
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/chat.postMessage")
    assert captured["json"] == {"channel": "C1", "text": "hi"}
    # 圧縮を避け raw=decode 済みを一致(デコーダのバッファで期限確認を素通りさせない。MAJ-002)。
    assert captured["headers"]["Accept-Encoding"] == "identity"


def test_live_http_caller_returns_slack_logical_error_dict(monkeypatch):
    # Slack は論理エラーでも HTTP 200 + {"ok": false}。caller はそのまま返し ok 判定はハンドラへ。
    _patch_client(
        monkeypatch, resp=_FakeStreamResp({"ok": False, "error": "channel_not_found"})
    )
    out = connector_runtime.live_http_caller("https://x", {}, {})
    assert out == {"ok": False, "error": "channel_not_found"}


def test_live_http_caller_network_error_fail_closed_no_token_leak(monkeypatch):
    import httpx

    exc = httpx.ConnectError("connect failed to secret-host xoxb-FAKE")
    _patch_client(monkeypatch, stream_exc=exc)
    with pytest.raises(ConnectorInvokeError) as ei:
        connector_runtime.live_http_caller(
            "https://x", {"Authorization": "Bearer xoxb-FAKE"}, {}
        )
    # 例外文に URL/本文/ヘッダ(トークン)を含めない。型名のみ。
    assert "xoxb-FAKE" not in str(ei.value)
    assert "ConnectError" in str(ei.value)
    # 連鎖(__cause__/__context__)を断つ: httpx.RequestError.request が Authorization を持つため、
    # 直接呼出でも連鎖経由でトークンが漏れない(MAJ-003)。
    assert ei.value.__cause__ is None
    assert ei.value.__context__ is None
    assert "xoxb-FAKE" not in repr(ei.value.__cause__)


def test_live_http_caller_non_json_fail_closed(monkeypatch):
    _patch_client(monkeypatch, resp=_FakeStreamResp(None, status=200, json_ok=False))
    with pytest.raises(connector_runtime.ConnectorTransportError) as ei:
        connector_runtime.live_http_caller("https://x", {}, {})
    assert "JSON" in str(ei.value)


# --- BE-03: live_http_caller の非 2xx は上流障害として倒す(MIN-001) ----------


def test_live_http_caller_non_2xx_with_json_body_fail_closed(monkeypatch):
    # 4xx/5xx が偶然 {"ok": true} を含んでも成功扱いにしない(非2xx は常に ConnectorTransportError)。
    _patch_client(monkeypatch, resp=_FakeStreamResp({"ok": True}, status=503))
    with pytest.raises(connector_runtime.ConnectorTransportError) as ei:
        connector_runtime.live_http_caller("https://x", {}, {})
    assert "503" in str(ei.value)


def test_live_http_caller_3xx_fail_closed(monkeypatch):
    # 3xx も成功扱いにしない(2xx 範囲外は ConnectorTransportError。MIN-001)。
    _patch_client(monkeypatch, resp=_FakeStreamResp({"ok": True}, status=302))
    with pytest.raises(connector_runtime.ConnectorTransportError) as ei:
        connector_runtime.live_http_caller("https://x", {}, {})
    assert "302" in str(ei.value)


# --- BE-03 MAJ-002: 絶対 wall-clock 期限とサイズ上限で trickle 応答を縛る ----------


def test_live_http_caller_wall_clock_deadline_exceeded_fail_closed(monkeypatch):
    # read timeout(無通信時間)を潜り抜ける trickle 応答でも、絶対 wall-clock 期限超過で打ち切る。
    # 期限を負にして「最初のチャンク受信時点で既に超過」を決定的に再現する(実 sleep に依存しない)。
    monkeypatch.setattr(connector_runtime, "_LIVE_HTTP_WALL_DEADLINE", -1.0)
    _patch_client(
        monkeypatch,
        resp=_FakeStreamResp(None, chunks=[b'{"ok"', b':true}']),
    )
    with pytest.raises(connector_runtime.ConnectorTransportError) as ei:
        connector_runtime.live_http_caller("https://x", {}, {})
    assert "wall-clock" in str(ei.value)


def test_live_http_caller_deadline_exceeded_after_eof_fail_closed(monkeypatch):
    # 最終チャンク後に EOF が遅延し期限を超えたケース: チャンクを yield せず(空ボディ=即 EOF)に
    # 期限超過 → ループ後の再確認で打ち切る(absolute 期限が EOF 遅延でも守られる。MAJ-002)。
    monkeypatch.setattr(connector_runtime, "_LIVE_HTTP_WALL_DEADLINE", -1.0)
    _patch_client(monkeypatch, resp=_FakeStreamResp(None, chunks=[]))
    with pytest.raises(connector_runtime.ConnectorTransportError) as ei:
        connector_runtime.live_http_caller("https://x", {}, {})
    assert "wall-clock" in str(ei.value)


def test_live_http_caller_response_too_large_fail_closed(monkeypatch):
    # サイズ上限超過も上流障害(502)に倒す(暴走/メモリ枯渇・trickle のサイズ面を縛る)。
    monkeypatch.setattr(connector_runtime, "_LIVE_HTTP_MAX_BYTES", 4)
    _patch_client(
        monkeypatch,
        resp=_FakeStreamResp(None, chunks=[b'{"ok":true,"channel":"C1","ts":"1.2"}']),
    )
    with pytest.raises(connector_runtime.ConnectorTransportError) as ei:
        connector_runtime.live_http_caller("https://x", {}, {})
    assert "大きすぎる" in str(ei.value)


# --- BE-03: Vault secret resolver(tenant＋plugin＋connector_id 束縛) ----------

SLACK_PID = "jetuse/slack-connector"
CONN = "conn-1"
CONN_B = "conn-2"
_OCID = "ocid1.vaultsecret.oc1..aaaa"


def _key(tenant, pid, cid, ref):
    return f"{tenant}/{pid}/{cid}/{ref}"


def _ocid_map(tenant=TENANT, pid=SLACK_PID, cid=CONN, ref="slack-bot-token", ocid=_OCID):
    return {_key(tenant, pid, cid, ref): ocid}


def _resolver_for(s, *, tenant=TENANT, pid=SLACK_PID, cid=CONN):
    return connector_runtime.make_vault_secret_resolver(
        s, tenant=tenant, plugin_id=pid, connector_id=cid
    )


def test_vault_secret_resolver_unmapped_ref_fail_closed():
    resolve = _resolver_for(Settings(connector_secret_ocids={}))
    with pytest.raises(connector_runtime.SecretResolutionError):
        resolve("slack-bot-token")


def test_vault_secret_resolver_reads_mapped_ocid(monkeypatch):
    # 合成キー `<tenant>/<plugin_id>/<connector_id>/<secretRef>` で引く。
    s = Settings(connector_secret_ocids=_ocid_map())
    seen = {}

    def _fake_read(ocid):
        seen["ocid"] = ocid
        return "xoxb-FROM-VAULT"

    monkeypatch.setattr(connector_runtime, "_read_vault_secret", _fake_read)
    assert _resolver_for(s)("slack-bot-token") == "xoxb-FROM-VAULT"
    assert seen["ocid"] == _OCID


def test_vault_secret_resolver_confused_deputy_blocked():
    # 別プラグインが同名 secretRef を宣言しても、他人(SLACK_PID)の OCID は引けない。
    s = Settings(connector_secret_ocids=_ocid_map())
    with pytest.raises(connector_runtime.SecretResolutionError):
        _resolver_for(s, pid="evil/impersonator")("slack-bot-token")


def test_vault_secret_resolver_cross_tenant_blocked():
    # 別テナントは同一 plugin/connector でも他テナントの secret を引けない(テナント束縛)。
    s = Settings(connector_secret_ocids=_ocid_map())
    with pytest.raises(connector_runtime.SecretResolutionError):
        _resolver_for(s, tenant=TENANT_B)("slack-bot-token")


def test_vault_secret_resolver_per_connector_isolation(monkeypatch):
    # 同一テナント/plugin でも connector_id ごとに別 OCID を解決、他 instance は引けない(BLK-001)。
    s = Settings(
        connector_secret_ocids={
            _key(TENANT, SLACK_PID, CONN, "slack-bot-token"): "ocid-A",
            _key(TENANT, SLACK_PID, CONN_B, "slack-bot-token"): "ocid-B",
        }
    )
    monkeypatch.setattr(
        connector_runtime, "_read_vault_secret", lambda ocid: f"tok-for-{ocid}"
    )
    assert _resolver_for(s, cid=CONN)("slack-bot-token") == "tok-for-ocid-A"
    assert _resolver_for(s, cid=CONN_B)("slack-bot-token") == "tok-for-ocid-B"
    # マップに無い 3 つめの connector は引けない(取り違え防止)。
    with pytest.raises(connector_runtime.SecretResolutionError):
        _resolver_for(s, cid="conn-3")("slack-bot-token")


def test_vault_secret_resolver_empty_value_503(monkeypatch):
    # Vault 復号値が空/空白 = サーバー側設定不備 → SecretResolutionError(=503。MIN-002)。
    s = Settings(connector_secret_ocids=_ocid_map())
    monkeypatch.setattr(connector_runtime, "_read_vault_secret", lambda ocid: "   ")
    with pytest.raises(connector_runtime.SecretResolutionError):
        _resolver_for(s)("slack-bot-token")


def test_vault_secret_resolver_strips_whitespace(monkeypatch):
    s = Settings(connector_secret_ocids=_ocid_map())
    monkeypatch.setattr(connector_runtime, "_read_vault_secret", lambda ocid: "  xoxb-PADDED  ")
    assert _resolver_for(s)("slack-bot-token") == "xoxb-PADDED"


def test_vault_secret_resolver_error_message_omits_ocid():
    resolve = _resolver_for(Settings(connector_secret_ocids={}))
    with pytest.raises(connector_runtime.SecretResolutionError) as ei:
        resolve("slack-bot-token")
    # 参照名(宣言・非機密)は出るが OCID 実値は出さない(未マップなので OCID も無い)。
    assert "slack-bot-token" in str(ei.value)


def test_read_vault_secret_sanitizes_and_clears_chain(monkeypatch):
    # OCI 例外に実 OCID/endpoint が載っても型名のみ露出し連鎖に残さない(MAJ-003)。
    import jetuse_core.plugins.connector_runtime as cr

    real_ocid = "ocid1.vaultsecret.oc1..SENSITIVE-LEAK"

    class _FakeSecrets:
        def __init__(self, *a, **k):
            pass

        def get_secret_bundle(self, ocid):
            raise RuntimeError(f"oci error referencing {real_ocid} at https://vaults.example")

    import oci

    monkeypatch.setattr(oci.secrets, "SecretsClient", _FakeSecrets)
    monkeypatch.setattr(oci.config, "from_file", lambda *a, **k: {})
    with pytest.raises(cr.SecretResolutionError) as ei:
        cr._read_vault_secret(real_ocid)
    assert real_ocid not in str(ei.value)
    assert ei.value.__cause__ is None
    assert ei.value.__context__ is None
    # 連鎖(__context__)経由でも漏れない。
    assert real_ocid not in repr(ei.value.__cause__)


def test_vault_secret_resolver_rejects_internal_whitespace_503(monkeypatch):
    # 内部空白を含む値は Bearer トークンとして不正 → SecretResolutionError(=503。MIN-001)。
    s = Settings(connector_secret_ocids=_ocid_map())
    monkeypatch.setattr(connector_runtime, "_read_vault_secret", lambda ocid: "xoxb FAKE")
    with pytest.raises(connector_runtime.SecretResolutionError):
        _resolver_for(s)("slack-bot-token")


def test_vault_secret_resolver_rejects_control_char_503(monkeypatch):
    s = Settings(connector_secret_ocids=_ocid_map())
    monkeypatch.setattr(connector_runtime, "_read_vault_secret", lambda ocid: "xoxb\nFAKE")
    with pytest.raises(connector_runtime.SecretResolutionError):
        _resolver_for(s)("slack-bot-token")
