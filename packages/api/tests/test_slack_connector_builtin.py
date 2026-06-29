"""コア同梱 Slack コネクタの単体テスト(CON-02)。

定義/manifest が CON-01 の検証(`validate_connector`/`validate_connector_composition`/
`validate_manifest`)を満たすこと、builtin ハンドラが登録され list_channels が動くことを確認する。
"""

from __future__ import annotations

from jetuse_core.platform_broker import issue_broker_token
from jetuse_core.plugins.connector import (
    ConnectorDefinition,
    validate_connector_composition,
)
from jetuse_core.plugins.connector_runtime import _BUILTIN_HANDLERS, invoke_connector_action
from jetuse_core.plugins.manifest import PluginManifest
from jetuse_core.plugins.slack_connector_builtin import (
    SLACK_CONNECTOR_ID,
    SLACK_LIST_CHANNELS_URL,
    SLACK_SECRET_REF,
    slack_connector_definition,
    slack_connector_manifest,
)
from jetuse_core.settings import Settings

TENANT = "ocid1.tenancy.oc1..aaaa-tenant-A"


def _settings() -> Settings:
    return Settings(platform_broker_secret="unit-broker-secret-32bytes-minimum!!")


def test_slack_definition_valid():
    d = slack_connector_definition()
    assert isinstance(d, ConnectorDefinition)
    assert d.provider == "slack"
    assert d.transport == "builtin"
    assert d.endpoint is None
    assert d.auth.kind == "oauth2"
    assert d.auth.secret_ref == SLACK_SECRET_REF
    assert {a.name for a in d.actions} == {"post_message", "list_channels"}


def test_slack_manifest_valid_connector_kind():
    m = slack_connector_manifest()
    assert isinstance(m, PluginManifest)
    assert m.kind == "connector"
    assert m.id == SLACK_CONNECTOR_ID


def test_slack_composition_ok_requires_secret():
    report = validate_connector_composition(slack_connector_manifest())
    assert report.ok is True
    assert report.undeclared_permissions == []
    assert report.requires_secret is True
    assert report.secret_ref == SLACK_SECRET_REF


def test_builtin_handlers_registered():
    assert ("slack", "post_message") in _BUILTIN_HANDLERS
    assert ("slack", "list_channels") in _BUILTIN_HANDLERS


def test_list_channels_ok():
    def http(url, headers, body):
        assert url == SLACK_LIST_CHANNELS_URL
        return {
            "ok": True,
            "channels": [{"id": "C1", "name": "general"}, {"id": "C2", "name": "random"}],
        }

    token = issue_broker_token(
        "jetuse/slack-connector", TENANT, ["platform:connector.invoke"], settings=_settings()
    )
    result = invoke_connector_action(
        slack_connector_definition(),
        "list_channels",
        {},
        broker_token=token,
        tenant=TENANT,
        settings=_settings(),
        secret_resolver=lambda ref: "xoxb-FAKE",
        http_caller=http,
    )
    assert result.ok is True
    assert [c["name"] for c in result.output["channels"]] == ["general", "random"]


# --- BE-03: connector.invoke を承認可能スコープに宣言(BLK-003) ----------------


def test_slack_manifest_declares_connector_invoke():
    m = slack_connector_manifest()
    # 正規 approve/issue 経路で invoke トークンを発行できるよう top-level permissions に宣言する。
    assert "platform:connector.invoke" in m.permissions
    report = validate_connector_composition(m)
    assert report.ok is True
    # connector.invoke は呼出権であり unused 扱いしない(action data-scope ではない)。
    assert "platform:connector.invoke" not in report.unused_permissions


# --- BE-03: Slack ok:false のエラー分類(MAJ-002) -----------------------------


def _invoke_with_slack_error(error_code):
    from jetuse_core.plugins.slack_connector_builtin import slack_connector_definition

    def http(url, headers, body):
        return {"ok": False, "error": error_code}

    token = issue_broker_token(
        "jetuse/slack-connector", TENANT, ["platform:connector.invoke"], settings=_settings()
    )
    return invoke_connector_action(
        slack_connector_definition(),
        "post_message",
        {"channel": "C1", "text": "hi"},
        broker_token=token,
        tenant=TENANT,
        settings=_settings(),
        secret_resolver=lambda ref: "xoxb-FAKE",
        http_caller=http,
    )


def test_slack_auth_error_maps_to_secret_resolution_error():
    import pytest

    from jetuse_core.plugins.connector_runtime import SecretResolutionError

    # invalid_auth / missing_scope 等 Bot トークン/scope 設定不備 → SecretResolutionError(=503)。
    for code in ("invalid_auth", "missing_scope", "token_revoked", "account_inactive"):
        with pytest.raises(SecretResolutionError):
            _invoke_with_slack_error(code)


def test_slack_upstream_error_maps_to_transport_error():
    import pytest

    from jetuse_core.plugins.connector_runtime import ConnectorTransportError

    # internal_error / ratelimited 等 上流一時障害 → ConnectorTransportError(=502)。
    for code in ("internal_error", "ratelimited", "service_unavailable"):
        with pytest.raises(ConnectorTransportError):
            _invoke_with_slack_error(code)


def test_slack_request_error_maps_to_invoke_error():
    import pytest

    from jetuse_core.plugins.connector_runtime import (
        ConnectorInvokeError,
        ConnectorTransportError,
        SecretResolutionError,
    )

    # channel_not_found 等 要求側の不備 → base ConnectorInvokeError(=400)。サブクラスではないこと。
    with pytest.raises(ConnectorInvokeError) as ei:
        _invoke_with_slack_error("channel_not_found")
    assert not isinstance(ei.value, (SecretResolutionError, ConnectorTransportError))


# --- BE-03 review-3: Slack 応答の厳密検証＋未知エラー安全側 (MAJ-001/MAJ-002) ----


def _invoke_post(http_resp_or_fn):
    fn = http_resp_or_fn if callable(http_resp_or_fn) else (lambda *a, **k: http_resp_or_fn)
    token = issue_broker_token(
        "jetuse/slack-connector", TENANT, ["platform:connector.invoke"], settings=_settings()
    )
    return invoke_connector_action(
        slack_connector_definition(),
        "post_message",
        {"channel": "C1", "text": "hi"},
        broker_token=token,
        tenant=TENANT,
        settings=_settings(),
        secret_resolver=lambda ref: "xoxb-FAKE",
        http_caller=fn,
    )


def test_slack_ok_must_be_boolean_true():
    import pytest

    from jetuse_core.plugins.connector_runtime import ConnectorTransportError

    # 文字列 "false" は truthy だが成功扱いにしない。error コードも無いので未知=安全側 502。
    with pytest.raises(ConnectorTransportError):
        _invoke_post({"ok": "false"})


def test_slack_success_schema_missing_ts_502():
    import pytest

    from jetuse_core.plugins.connector_runtime import ConnectorTransportError

    # ok:true でも ts/channel 欠落は上流スキーマ不一致 → 502。
    with pytest.raises(ConnectorTransportError):
        _invoke_post({"ok": True, "channel": "C1"})  # ts 欠落


def test_slack_unknown_error_code_maps_to_transport_error():
    import pytest

    from jetuse_core.plugins.connector_runtime import ConnectorTransportError

    # allowlist 外の未知コードは恒久 400 に潰さず安全側 502(将来の新コードを誤分類しない)。
    with pytest.raises(ConnectorTransportError):
        _invoke_post({"ok": False, "error": "some_future_unlisted_error"})


# --- BE-03 review-4: list_channels の cursor ページング＋要素検証 (MAJ-002/MIN-001) -


def _invoke_list(http_fn):
    token = issue_broker_token(
        "jetuse/slack-connector", TENANT, ["platform:connector.invoke"], settings=_settings()
    )
    return invoke_connector_action(
        slack_connector_definition(),
        "list_channels",
        {},
        broker_token=token,
        tenant=TENANT,
        settings=_settings(),
        secret_resolver=lambda ref: "xoxb-FAKE",
        http_caller=http_fn,
    )


def test_list_channels_paginates_and_requests_private():
    # 2 ページを next_cursor で辿り全件返す。types に private_channel を含める。
    pages = [
        {"ok": True, "channels": [{"id": "C1", "name": "general"}],
         "response_metadata": {"next_cursor": "CUR2"}},
        {"ok": True, "channels": [{"id": "C2", "name": "private-x"}],
         "response_metadata": {"next_cursor": ""}},
    ]
    seen_bodies = []
    state = {"i": 0}

    def http(url, headers, body):
        seen_bodies.append(body)
        resp = pages[state["i"]]
        state["i"] += 1
        return resp

    result = _invoke_list(http)
    assert [c["name"] for c in result.output["channels"]] == ["general", "private-x"]
    assert result.output["truncated"] is False
    # types に public/private 両方、2 ページ目は cursor 送出。
    assert "private_channel" in seen_bodies[0]["types"]
    assert seen_bodies[1]["cursor"] == "CUR2"


def test_list_channels_truncates_at_cap():
    # 常に next_cursor を返す上流では上限で止め、truncated=True を明示する(無言切り捨て禁止)。
    def http(url, headers, body):
        return {"ok": True, "channels": [{"id": "C", "name": "n"}],
                "response_metadata": {"next_cursor": "more"}}

    result = _invoke_list(http)
    assert result.output["truncated"] is True


def test_list_channels_bad_element_502():
    import pytest

    from jetuse_core.plugins.connector_runtime import ConnectorTransportError

    # id 欠落/非文字列の要素は黙って捨てず上流スキーマ不一致(502)。
    def http(url, headers, body):
        return {"ok": True, "channels": [{"name": "missing-id"}], "response_metadata": {}}

    with pytest.raises(ConnectorTransportError):
        _invoke_list(http)


def test_slack_post_empty_channel_ts_502():
    import pytest

    from jetuse_core.plugins.connector_runtime import ConnectorTransportError

    # 空文字の channel/ts は投稿成功に見せない → 上流スキーマ不一致(502)。MIN-002。
    with pytest.raises(ConnectorTransportError):
        _invoke_post({"ok": True, "channel": "", "ts": ""})


def test_list_channels_metadata_missing_is_complete():
    # response_metadata 欠落は「これ以上ページなし(完全な一覧)」として許容する。
    def http(url, headers, body):
        return {"ok": True, "channels": [{"id": "C1", "name": "general"}]}

    result = _invoke_list(http)
    assert result.output["truncated"] is False
    assert [c["name"] for c in result.output["channels"]] == ["general"]


def test_list_channels_metadata_non_dict_502():
    import pytest

    from jetuse_core.plugins.connector_runtime import ConnectorTransportError

    # response_metadata が dict でない壊れた応答を「完全な一覧」に化けさせない → 502(MIN-001)。
    def http(url, headers, body):
        return {"ok": True, "channels": [{"id": "C1", "name": "g"}], "response_metadata": []}

    with pytest.raises(ConnectorTransportError):
        _invoke_list(http)


def test_list_channels_next_cursor_non_string_502():
    import pytest

    from jetuse_core.plugins.connector_runtime import ConnectorTransportError

    # next_cursor が文字列でない(例: 数値)壊れた応答も 502(MIN-001)。
    def http(url, headers, body):
        return {
            "ok": True,
            "channels": [{"id": "C1", "name": "g"}],
            "response_metadata": {"next_cursor": 123},
        }

    with pytest.raises(ConnectorTransportError):
        _invoke_list(http)


def test_list_channels_stops_at_wall_clock_deadline(monkeypatch):
    # 上流が常に next_cursor を返しても、全体 wall-clock 期限を超えたらページ上限(50)より前に
    # 打ち切り、truncated=True を明示する(API Gateway 上限超過の占有を防ぐ。MAJ-002)。
    from jetuse_core.plugins import slack_connector_builtin as scb

    # 2 ページ目処理後の deadline 確認で期限超過とみなすよう monotonic を進める。
    ticks = iter([0.0, scb.SLACK_LIST_DEADLINE_SECONDS + 1.0])

    def _fake_monotonic():
        try:
            return next(ticks)
        except StopIteration:
            return scb.SLACK_LIST_DEADLINE_SECONDS + 1.0

    monkeypatch.setattr(scb.time, "monotonic", _fake_monotonic)
    calls = {"n": 0}

    def http(url, headers, body):
        calls["n"] += 1
        return {"ok": True, "channels": [{"id": f"C{calls['n']}", "name": "n"}],
                "response_metadata": {"next_cursor": "more"}}

    result = _invoke_list(http)
    assert result.output["truncated"] is True
    # 期限超過で 1 ページ取得後に打ち切る(上限 50 まで回さない)。
    assert calls["n"] == 1
