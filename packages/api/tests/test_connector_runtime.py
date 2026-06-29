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
        # 中央 invoke 境界が裏取りする呼出し記録（認可 action の完全一致呼出し。BE06-MAJ-001）。
        return {"ok": True, "mcp": True, "output_text": "done",
                "calls": [{"name": action, "status": "completed", "arguments": payload}]}

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
        return {"ok": True,
                "calls": [{"name": action, "status": "completed", "arguments": payload}]}

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


def test_default_mcp_caller_is_fail_closed():
    """既定 MCP caller は実行せず拒否する（BE06-BLK-001。実 MCP 直結 transport は人間ゲート）。"""
    spec = {"type": "mcp", "server_label": "teams", "server_url": "https://x",
            "require_approval": "never"}
    with pytest.raises(ConnectorInvokeError):
        connector_runtime._default_mcp_caller(spec, "send", {"a": 1})


def test_mcp_tool_was_called_rejects_unauthorized_second_call():
    """認可 action 以外の MCP 呼出しが在れば正常 call が在っても fail-closed（BE06-BLK-001）。"""
    item_ok = {"type": "mcp_call", "name": "send", "status": "completed", "arguments": {"a": 1}}
    item_bad = {"type": "mcp_call", "name": "exfiltrate", "status": "completed", "arguments": {}}
    resp = type("R", (), {"output_text": "ok", "output": [item_ok, item_bad]})()
    assert connector_runtime._mcp_tool_was_called(resp, "send", {"a": 1}) is False


def _resp_with_args(args):
    """arguments を伴う mcp_call 応答を模す（_args_match_payload の検査対象）。"""
    item = {"type": "mcp_call", "name": "send", "status": "completed", "arguments": args}
    return type("R", (), {"output_text": "ok", "output": [item]})()


def test_mcp_args_match_payload_accepts_exact():
    """実引数が認可 payload を完全一致で含めば ok（BE06-R003）。"""
    resp = _resp_with_args({"q": "hello", "k": 3})
    assert connector_runtime._mcp_tool_was_called(resp, "send", {"q": "hello", "k": 3}) is True


def test_mcp_args_match_payload_rejects_tampered():
    """モデルが引数を改変（値が違う）したら fail-closed（BE06-R003）。"""
    resp = _resp_with_args({"q": "EVIL", "k": 3})
    assert connector_runtime._mcp_tool_was_called(resp, "send", {"q": "hello", "k": 3}) is False


def test_mcp_args_match_payload_rejects_missing_key():
    """認可した引数キーが欠落したら fail-closed（BE06-R003）。"""
    resp = _resp_with_args({"q": "hello"})
    assert connector_runtime._mcp_tool_was_called(resp, "send", {"q": "hello", "k": 3}) is False


def test_mcp_args_match_payload_parses_json_string():
    """arguments が JSON 文字列でも解して照合する（BE06-R003）。"""
    resp = _resp_with_args('{"q": "hello", "k": 3}')
    assert connector_runtime._mcp_tool_was_called(resp, "send", {"q": "hello", "k": 3}) is True


def test_mcp_args_match_payload_rejects_extra_key():
    """モデルが認可外の引数（tenant/limit 等）を追加したら fail-closed（BE06-REV-004）。"""
    resp = _resp_with_args({"q": "hello", "k": 3, "tenant": "other"})
    assert connector_runtime._mcp_tool_was_called(resp, "send", {"q": "hello", "k": 3}) is False


def test_mcp_args_absent_is_fail_closed():
    """arguments が応答に載らない（照合不能）なら fail-closed（改変を通さない。BE06-REV-004）。"""
    item = {"type": "mcp_call", "name": "send", "status": "completed"}
    resp = type("R", (), {"output_text": "ok", "output": [item]})()
    assert connector_runtime._mcp_tool_was_called(resp, "send", {"q": "hello"}) is False


def test_mcp_boundary_rejects_bare_ok():
    """中央 invoke 境界は呼出し記録の無い {"ok": true} を成功にしない（BE06-MAJ-001）。"""
    def bare_ok_caller(spec, action, payload):
        return {"ok": True, "hits": ["anything"]}  # calls/output 記録が無い

    with pytest.raises(ConnectorInvokeError):
        invoke_connector_action(
            _mcp_def(),
            "send",
            {"x": 1},
            broker_token=_token(["platform:connector.invoke"]),
            tenant=TENANT,
            settings=_settings(),
            secret_resolver=lambda ref: FAKE_TOKEN,
            mcp_caller=bare_ok_caller,
        )


def test_mcp_boundary_rejects_cross_tool_call():
    """記録に認可 action 以外の呼出しが混ざれば境界で fail-closed（越境。BE06-MAJ-001）。"""
    def cross_caller(spec, action, payload):
        return {"ok": True, "calls": [
            {"name": action, "status": "completed", "arguments": payload},
            {"name": "exfiltrate", "status": "completed", "arguments": {}},
        ]}

    with pytest.raises(ConnectorInvokeError):
        invoke_connector_action(
            _mcp_def(),
            "send",
            {"x": 1},
            broker_token=_token(["platform:connector.invoke"]),
            tenant=TENANT,
            settings=_settings(),
            secret_resolver=lambda ref: FAKE_TOKEN,
            mcp_caller=cross_caller,
        )


def test_mcp_boundary_rejects_tampered_args():
    """記録の実引数が認可 payload と食い違えば境界で fail-closed（改変。BE06-MAJ-001）。"""
    def tamper_caller(spec, action, payload):
        return {"ok": True, "calls": [
            {"name": action, "status": "completed", "arguments": {"x": 999}},
        ]}

    with pytest.raises(ConnectorInvokeError):
        invoke_connector_action(
            _mcp_def(),
            "send",
            {"x": 1},
            broker_token=_token(["platform:connector.invoke"]),
            tenant=TENANT,
            settings=_settings(),
            secret_resolver=lambda ref: FAKE_TOKEN,
            mcp_caller=tamper_caller,
        )


def test_mcp_boundary_accepts_responses_output_form():
    """Responses 形式（output 列）の呼出し記録も境界が裏取りして受理する（BE06-MAJ-001）。"""
    def responses_caller(spec, action, payload):
        return {"ok": True, "output": [
            {"type": "mcp_call", "name": action, "status": "completed", "arguments": payload},
        ], "result": "done"}

    result = invoke_connector_action(
        _mcp_def(),
        "send",
        {"x": 1},
        broker_token=_token(["platform:connector.invoke"]),
        tenant=TENANT,
        settings=_settings(),
        secret_resolver=lambda ref: FAKE_TOKEN,
        mcp_caller=responses_caller,
    )
    assert result.ok is True


def test_args_match_payload_type_strict_bool_vs_int():
    """_json_equal は True と 1 を区別する（BE06-MIN-001）。"""
    item_true = {"type": "mcp_call", "name": "send", "status": "completed",
                 "arguments": {"flag": True}}
    resp_true = type("R", (), {"output": [item_true]})()
    # 認可 payload が {"flag": 1}（int）なら True（bool）の引数は不一致 → fail-closed。
    assert connector_runtime._mcp_tool_was_called(resp_true, "send", {"flag": 1}) is False
    # 同型（bool）なら一致。
    assert connector_runtime._mcp_tool_was_called(resp_true, "send", {"flag": True}) is True


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
        return {"ok": True,
                "calls": [{"name": action, "status": "completed", "arguments": payload}]}

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
        return {"ok": True, "echo": spec["headers"]["Authorization"],
                "calls": [{"name": action, "status": "completed", "arguments": payload}]}

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
        return {"ok": True, spec["headers"]["Authorization"]: "x",
                "calls": [{"name": action, "status": "completed", "arguments": payload}]}

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
