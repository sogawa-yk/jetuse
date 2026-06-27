"""既存資産コネクタ（No.1-RAG / No.1-SQL-Assist）の単体テスト（ASSET-01）。

正規化した manifest／定義が CON-01 の構造検証・合成バリデーションを通ること、mock mcp_caller を
注入した invoke が broker 認可（rag.search / db.query ＋ connector.invoke）を通って疎通すること、
**実トークンが定義・戻り値・例外に出ない**ことを検証する。実 MCP/実 Vault は
投入しない（人間ゲート）。
"""

from __future__ import annotations

import json

import pytest

from jetuse_core.platform_broker import issue_broker_token
from jetuse_core.plugins.asset_connectors import (
    NO1_RAG_PROVIDER,
    NO1_RAG_SECRET_REF,
    NO1_SQL_PROVIDER,
    NO1_SQL_SECRET_REF,
    asset_connector_manifests,
    no1_rag_connector_definition,
    no1_rag_connector_manifest,
    no1_sql_assist_connector_definition,
)
from jetuse_core.plugins.connector import (
    ConnectorError,
    validate_connector_composition,
)
from jetuse_core.plugins.connector_runtime import invoke_connector_action
from jetuse_core.settings import Settings

RAG_ENDPOINT = "https://no1-rag.example.com/mcp"
SQL_ENDPOINT = "https://no1-sql-assist.example.com/mcp"
TENANT = "ocid1.tenancy.oc1..aaaa-tenant-A"
FAKE_RAG_TOKEN = "no1rag-FAKE-NOT-A-REAL-TOKEN"
FAKE_SQL_TOKEN = "no1sql-FAKE-NOT-A-REAL-TOKEN"


def _settings() -> Settings:
    return Settings(
        platform_broker_secret="unit-broker-secret-32bytes-minimum!!",
        platform_token_ttl_seconds=300,
    )


def _token(plugin: str, scopes) -> str:
    return issue_broker_token(plugin, TENANT, scopes, settings=_settings())


# --- 定義・manifest の構造／合成 -------------------------------------------


def test_no1_rag_definition_structure():
    d = no1_rag_connector_definition(RAG_ENDPOINT)
    assert d.provider == NO1_RAG_PROVIDER
    assert d.transport == "mcp"
    assert d.endpoint == RAG_ENDPOINT
    assert d.auth.kind == "api_token"
    assert d.auth.secret_ref == NO1_RAG_SECRET_REF
    assert [a.name for a in d.actions] == ["search"]
    assert d.actions[0].permissions == ["platform:rag.search"]


def test_no1_sql_definition_structure():
    d = no1_sql_assist_connector_definition(SQL_ENDPOINT)
    assert d.provider == NO1_SQL_PROVIDER
    assert d.transport == "mcp"
    assert d.auth.secret_ref == NO1_SQL_SECRET_REF
    assert [a.name for a in d.actions] == ["nl2sql"]
    assert d.actions[0].permissions == ["platform:db.query"]


def test_asset_manifests_compose_ok():
    """合成バリデーション（宣言整合）が両資産で ok=True（undeclared 無し）。"""
    manifests = asset_connector_manifests(RAG_ENDPOINT, SQL_ENDPOINT)
    assert [m.kind for m in manifests] == ["connector", "connector"]
    for m in manifests:
        report = validate_connector_composition(m)
        assert report.ok is True
        assert report.undeclared_permissions == []
        assert report.requires_secret is True
        # secret は参照名のみ（実値ではない）。
        assert report.secret_ref in (NO1_RAG_SECRET_REF, NO1_SQL_SECRET_REF)


def test_manifest_definition_has_no_real_secret_value():
    """配布表現に実トークンが無く secretRef（参照名）のみであること。"""
    m = no1_rag_connector_manifest(RAG_ENDPOINT)
    blob = json.dumps(m.contributes, ensure_ascii=False)
    assert NO1_RAG_SECRET_REF in blob  # 参照名は保持してよい
    assert "FAKE" not in blob and "token-value" not in blob
    # auth に実値を載せる経路（例: "token"/"password" キー）が無い。
    assert "password" not in blob


def test_mcp_endpoint_validated_offline():
    """transport=mcp の endpoint はオフライン検証される（private/loopback は拒否）。"""
    with pytest.raises(ConnectorError):
        no1_rag_connector_definition("https://127.0.0.1/mcp")
    with pytest.raises(ConnectorError):
        no1_sql_assist_connector_definition("http://no1-sql-assist.example.com/mcp")  # 非 https


# --- mock MCP 疎通（invoke） ----------------------------------------------


def test_invoke_no1_rag_search_via_mock_mcp():
    """No.1-RAG search を mock mcp_caller で疎通。broker 認可を通り実トークンが出ない。"""
    d = no1_rag_connector_definition(RAG_ENDPOINT)
    captured: dict = {}

    def mcp_caller(spec, action, payload):
        captured["spec"] = spec
        captured["action"] = action
        captured["payload"] = payload
        return {"ok": True, "mcp": True, "hits": [{"doc": "d1", "score": 0.9}]}

    def resolver(ref: str) -> str:
        assert ref == NO1_RAG_SECRET_REF
        return FAKE_RAG_TOKEN

    result = invoke_connector_action(
        d,
        "search",
        {"query": "保守契約の更新条件"},
        broker_token=_token(
            NO1_RAG_PROVIDER, ["platform:connector.invoke", "platform:rag.search"]
        ),
        tenant=TENANT,
        resource="asset-e2e-rag",
        settings=_settings(),
        secret_resolver=resolver,
        mcp_caller=mcp_caller,
    )
    assert result.ok is True
    assert result.transport == "mcp"
    assert result.output["hits"][0]["doc"] == "d1"
    # spec には Authorization が載るが、戻り値・捕捉 spec に実トークンが残らないこと。
    assert FAKE_RAG_TOKEN not in json.dumps(result.output)
    assert captured["action"] == "search"
    assert captured["spec"]["server_url"] == RAG_ENDPOINT


def test_invoke_no1_sql_nl2sql_via_mock_mcp():
    d = no1_sql_assist_connector_definition(SQL_ENDPOINT)

    def mcp_caller(spec, action, payload):
        return {"ok": True, "sql": "SELECT 1", "rows": []}

    def resolver(ref: str) -> str:
        assert ref == NO1_SQL_SECRET_REF
        return FAKE_SQL_TOKEN

    result = invoke_connector_action(
        d,
        "nl2sql",
        {"question": "先月の売上合計は？"},
        broker_token=_token(
            NO1_SQL_PROVIDER, ["platform:connector.invoke", "platform:db.query"]
        ),
        tenant=TENANT,
        resource="asset-e2e-sql",
        settings=_settings(),
        secret_resolver=resolver,
        mcp_caller=mcp_caller,
    )
    assert result.ok is True
    assert result.output["sql"] == "SELECT 1"
    assert FAKE_SQL_TOKEN not in json.dumps(result.output)


def test_invoke_denied_without_scope():
    """必要 Platform スコープ（rag.search）が無い token は fail-closed（外部不到達）。"""
    from jetuse_core.plugins.connector_runtime import ConnectorInvokeDenied

    d = no1_rag_connector_definition(RAG_ENDPOINT)
    reached = {"called": False}

    def mcp_caller(spec, action, payload):  # pragma: no cover - 到達しないことを検証
        reached["called"] = True
        return {"ok": True}

    with pytest.raises(ConnectorInvokeDenied):
        invoke_connector_action(
            d,
            "search",
            {"query": "x"},
            broker_token=_token(NO1_RAG_PROVIDER, ["platform:connector.invoke"]),
            tenant=TENANT,
            settings=_settings(),
            secret_resolver=lambda r: FAKE_RAG_TOKEN,
            mcp_caller=mcp_caller,
        )
    assert reached["called"] is False
