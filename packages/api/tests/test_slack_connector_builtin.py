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
