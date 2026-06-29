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
        # 中央 invoke 境界が裏取りする呼出し記録（BE06-MAJ-001）。
        return {"ok": True, "mcp": True, "hits": [{"doc": "d1", "score": 0.9}],
                "calls": [{"name": action, "status": "completed", "arguments": payload}]}

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
        return {"ok": True, "sql": "SELECT 1", "rows": [],
                "calls": [{"name": action, "status": "completed", "arguments": payload}]}

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


# --- invoke 配線ヘルパ（invoke_no1_* / BE-06） -----------------------------


def test_invoke_no1_rag_search_helper():
    """invoke_no1_rag_search が定義組み立て＋broker 認可＋mock MCP 疎通を一括で行う。"""
    from jetuse_core.plugins.asset_connectors import invoke_no1_rag_search

    captured = {}

    def mcp_caller(spec, action, payload):
        captured["action"] = action
        captured["payload"] = payload
        captured["server_url"] = spec["server_url"]
        return {"ok": True, "hits": [{"doc": "d1"}],
                "calls": [{"name": action, "status": "completed", "arguments": payload}]}

    result = invoke_no1_rag_search(
        RAG_ENDPOINT,
        "保守契約の更新条件",
        broker_token=_token(
            NO1_RAG_PROVIDER, ["platform:connector.invoke", "platform:rag.search"]
        ),
        tenant=TENANT,
        resource="asset-e2e-rag",
        settings=_settings(),
        secret_resolver=lambda r: FAKE_RAG_TOKEN,
        mcp_caller=mcp_caller,
        top_k=3,
    )
    assert result.ok is True
    assert captured["action"] == "search"
    assert captured["payload"] == {"query": "保守契約の更新条件", "top_k": 3}
    assert captured["server_url"] == RAG_ENDPOINT
    assert FAKE_RAG_TOKEN not in json.dumps(result.output)


def test_invoke_no1_sql_nl2sql_helper():
    from jetuse_core.plugins.asset_connectors import invoke_no1_sql_nl2sql

    def mcp_caller(spec, action, payload):
        assert action == "nl2sql"
        assert payload["question"] == "先月の売上合計は？"
        return {"ok": True, "sql": "SELECT 1",
                "calls": [{"name": action, "status": "completed", "arguments": payload}]}

    result = invoke_no1_sql_nl2sql(
        SQL_ENDPOINT,
        "先月の売上合計は？",
        broker_token=_token(
            NO1_SQL_PROVIDER, ["platform:connector.invoke", "platform:db.query"]
        ),
        tenant=TENANT,
        settings=_settings(),
        secret_resolver=lambda r: FAKE_SQL_TOKEN,
        mcp_caller=mcp_caller,
    )
    assert result.output["sql"] == "SELECT 1"


def test_invoke_helper_denied_without_scope():
    """ヘルパ経由でも必要スコープ無しは fail-closed（外部不到達）。"""
    from jetuse_core.plugins.asset_connectors import invoke_no1_rag_search
    from jetuse_core.plugins.connector_runtime import ConnectorInvokeDenied

    reached = {"called": False}

    def mcp_caller(spec, action, payload):  # pragma: no cover - 到達しない
        reached["called"] = True
        return {"ok": True}

    with pytest.raises(ConnectorInvokeDenied):
        invoke_no1_rag_search(
            RAG_ENDPOINT,
            "x",
            broker_token=_token(NO1_RAG_PROVIDER, ["platform:connector.invoke"]),
            tenant=TENANT,
            settings=_settings(),
            secret_resolver=lambda r: FAKE_RAG_TOKEN,
            mcp_caller=mcp_caller,
        )
    assert reached["called"] is False


# --- Vault 解決の継ぎ目（vault_secret_resolver） ---------------------------


def test_vault_secret_resolver_reads_via_vault(monkeypatch):
    """vault_secret_resolver は secretRef→OCID→実値を Vault 経由（_read_secret）で解決する。"""
    import jetuse_core.mcp_servers as mcp_servers
    from jetuse_core.plugins.asset_connectors import vault_secret_resolver

    reads = []
    monkeypatch.setattr(
        mcp_servers, "_read_secret", lambda ocid: reads.append(ocid) or FAKE_RAG_TOKEN
    )
    resolver = vault_secret_resolver({NO1_RAG_SECRET_REF: "ocid1.vaultsecret.oc1..aaaa"})
    assert resolver(NO1_RAG_SECRET_REF) == FAKE_RAG_TOKEN
    assert reads == ["ocid1.vaultsecret.oc1..aaaa"]


def test_vault_secret_resolver_unknown_ref_fail_closed():
    """未登録 secretRef は fail-closed（KeyError）。実値は出さない。"""
    from jetuse_core.plugins.asset_connectors import vault_secret_resolver

    resolver = vault_secret_resolver({NO1_RAG_SECRET_REF: "ocid1.x"})
    with pytest.raises(KeyError):
        resolver("unknown-ref")


# --- secret 解決例外の正規化（M-002） / MCP 最小権限・呼出検証（B-003） -------


def test_invoke_helper_unknown_secret_ref_normalized():
    """vault_secret_resolver の未知 ref（KeyError）は ConnectorInvokeError に正規化される。"""
    from jetuse_core.plugins.asset_connectors import (
        invoke_no1_rag_search,
        vault_secret_resolver,
    )
    from jetuse_core.plugins.connector_runtime import ConnectorInvokeError

    resolver = vault_secret_resolver({})  # 何も登録しない → どの ref も未知
    with pytest.raises(ConnectorInvokeError):
        invoke_no1_rag_search(
            RAG_ENDPOINT, "x",
            broker_token=_token(
                NO1_RAG_PROVIDER,
                ["platform:connector.invoke", "platform:rag.search"],
            ),
            tenant=TENANT, settings=_settings(),
            secret_resolver=resolver,
            mcp_caller=lambda s, a, p: {"ok": True},
        )


def test_invoke_helper_resolver_transient_error_normalized():
    """resolver の一時障害（任意例外）も ConnectorInvokeError へ正規化（連鎖に実値を残さない）。"""
    from jetuse_core.plugins.asset_connectors import invoke_no1_rag_search
    from jetuse_core.plugins.connector_runtime import ConnectorInvokeError

    def boom(ref):
        raise RuntimeError(f"vault transient secret={FAKE_RAG_TOKEN}")

    with pytest.raises(ConnectorInvokeError) as ei:
        invoke_no1_rag_search(
            RAG_ENDPOINT, "x",
            broker_token=_token(
                NO1_RAG_PROVIDER,
                ["platform:connector.invoke", "platform:rag.search"],
            ),
            tenant=TENANT, settings=_settings(),
            secret_resolver=boom,
            mcp_caller=lambda s, a, p: {"ok": True},
        )
    assert FAKE_RAG_TOKEN not in str(ei.value)
    assert ei.value.__cause__ is None


def test_mcp_spec_restricts_allowed_tools():
    """invoke が組む MCP spec は allowed_tools を認可 action に絞る（最小権限・B-003）。"""
    from jetuse_core.plugins.asset_connectors import invoke_no1_rag_search

    captured = {}
    invoke_no1_rag_search(
        RAG_ENDPOINT, "q",
        broker_token=_token(NO1_RAG_PROVIDER, ["platform:connector.invoke", "platform:rag.search"]),
        tenant=TENANT, settings=_settings(),
        secret_resolver=lambda r: FAKE_RAG_TOKEN,
        mcp_caller=lambda s, a, p: captured.update(spec=s)
        or {"ok": True, "calls": [{"name": a, "status": "completed", "arguments": p}]},
    )
    assert captured["spec"]["allowed_tools"] == ["search"]


def test_default_mcp_caller_rejects_uncalled_and_wrong_tool():
    """既定 caller の検証ヘルパ: 認可 action の実呼出が無い応答は拒否（別ツール/無呼出/エラー）。"""
    from jetuse_core.plugins.connector_runtime import _mcp_tool_was_called

    class Resp:
        def __init__(self, output):
            self.output = output

    # 呼ばれた（dict 形）
    assert _mcp_tool_was_called(Resp([{"type": "mcp_call", "name": "search"}]), "search") is True
    # 別ツールが呼ばれた
    wrong = Resp([{"type": "mcp_call", "name": "delete_all"}])
    assert _mcp_tool_was_called(wrong, "search") is False
    # 何も呼ばれていない（平文回答のみ）
    assert _mcp_tool_was_called(Resp([{"type": "message"}]), "search") is False
    # 呼ばれたがエラー
    assert _mcp_tool_was_called(
        Resp([{"type": "mcp_call", "name": "search", "error": "boom"}]), "search"
    ) is False
    # status=failed / incomplete は成功扱いしない（MCP-001）
    assert _mcp_tool_was_called(
        Resp([{"type": "mcp_call", "name": "search", "status": "failed"}]), "search"
    ) is False
    assert _mcp_tool_was_called(
        Resp([{"type": "mcp_call", "name": "search", "status": "incomplete"}]), "search"
    ) is False
    # status=completed は受理
    assert _mcp_tool_was_called(
        Resp([{"type": "mcp_call", "name": "search", "status": "completed"}]), "search"
    ) is True


def test_invoke_helper_validates_input_before_authz():
    """主入力の型/長さ・payload の JSON 化・総サイズを認可前に検証（BOUNDARY-001）。"""
    from jetuse_core.plugins.asset_connectors import invoke_no1_rag_search
    from jetuse_core.plugins.connector_runtime import ConnectorInvokeError

    reached = {"called": False}

    def mcp(spec, action, payload):  # pragma: no cover - 検証で弾かれ到達しない
        reached["called"] = True
        return {"ok": True}

    common = dict(
        broker_token=_token(NO1_RAG_PROVIDER, ["platform:connector.invoke", "platform:rag.search"]),
        tenant=TENANT, settings=_settings(),
        secret_resolver=lambda r: FAKE_RAG_TOKEN, mcp_caller=mcp,
    )
    with pytest.raises(ConnectorInvokeError):  # 空 query
        invoke_no1_rag_search(RAG_ENDPOINT, "  ", **common)
    with pytest.raises(ConnectorInvokeError):  # 長すぎる query
        invoke_no1_rag_search(RAG_ENDPOINT, "x" * 50000, **common)
    with pytest.raises(ConnectorInvokeError):  # JSON 化できない追加 payload
        invoke_no1_rag_search(RAG_ENDPOINT, "q", extra={1, 2, 3}, **common)
    assert reached["called"] is False  # いずれも認可・解決前に弾かれ MCP へ到達しない
