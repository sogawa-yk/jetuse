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
