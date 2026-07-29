"""GAP-04: マネージド・ホスト型エージェント連携の単体テスト(HTTP/IDCSはモック)"""

import pytest

from jetuse_core import hosted_agent
from jetuse_core.settings import get_settings


@pytest.fixture(autouse=True)
def reset():
    get_settings.cache_clear()
    hosted_agent._token.update({"value": None, "exp": 0.0})
    yield
    get_settings.cache_clear()


def test_not_configured_raises():
    with pytest.raises(hosted_agent.HostedAgentNotConfigured):
        hosted_agent.invoke("hello")


def test_agent_create_hosted_ignores_tools(monkeypatch):
    """ADR-0009: hostedルーティングのSDK(openai_agents)はツール無し定義を受理する。
    旧framework値 'hosted' は現行Literalから撤廃済み → openai_agents へ置換。"""
    from fastapi.testclient import TestClient

    from service.main import app

    client = TestClient(app)
    res = client.post("/api/agents", json={
        "name": "managed", "instructions": "x", "model": "gpt-oss-120b",
        "framework": "openai_agents",
        "enabled_tools": [],
    })
    assert res.status_code != 422


# --- PORT-03: invoke ステートに載せる project OCID の決め方 ---
# 非同期テストプラグインは入れていないので、コルーチンは asyncio.run で回す。


def _user():
    from jetuse_core.auth import AuthContext

    return AuthContext(subject="dev-user")


def _chat_request(text: str = "こんにちは"):
    from service.schemas import ChatRequest

    return ChatRequest(model="gpt-oss-120b", messages=[{"role": "user", "content": text}])


@pytest.fixture
def deployed(monkeypatch):
    """3SDK が配備済みの状態。未配備だと dispatch は invoke 前に理由付きで縮退する。"""
    from jetuse_core.settings import get_settings

    for k, v in {
        "HOSTED_AGENT_IDCS_DOMAIN": "https://idcs-test.identity.oraclecloud.com",
        "HOSTED_AGENT_CLIENT_ID": "cid",
        "HOSTED_AGENT_CLIENT_SECRET": "secret",
        "HOSTED_AGENT_SCOPE": "jetuse-agentinvoke",
        "AGENT_OPENAI_APP_OCID": "ocid1.generativeaihostedapplication.oc1..openai",
        "AGENT_LANGGRAPH_APP_OCID": "ocid1.generativeaihostedapplication.oc1..langgraph",
        "AGENT_ADK_APP_OCID": "ocid1.generativeaihostedapplication.oc1..adk",
    }.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _run_dispatch(agent_def: dict) -> str:
    import asyncio

    from service import agent_dispatch

    async def go():
        res = await agent_dispatch.hosted_agent_stream_response(
            _chat_request(), _user(), agent_def)
        return "".join([c async for c in res.body_iterator])

    return asyncio.run(go())


def test_dispatch_prefers_agent_assigned_project(monkeypatch, deployed):
    """SPIKE-05: エージェントに Project が割り当てられていれば自動解決で上書きしない
    (上書きするとそのエージェントの会話・記憶の分離が壊れる)。"""
    from jetuse_core import genai
    from service import agent_dispatch

    seen: dict = {}
    monkeypatch.setattr(agent_dispatch.hosted_agent, "invoke_agent",
                        lambda sdk, state: seen.update(state) or {"output": "ok"})
    monkeypatch.setattr(genai, "resolve_project_ocid",
                        lambda *a, **kw: pytest.fail("must not auto-resolve"))

    _run_dispatch({"framework": "openai_agents", "project_ocid": "ocid1.project.oc1..assigned"})
    assert seen["project_ocid"] == "ocid1.project.oc1..assigned"


def test_dispatch_falls_back_to_resolved_project(monkeypatch, deployed):
    from jetuse_core import genai
    from service import agent_dispatch

    seen: dict = {}
    monkeypatch.setattr(agent_dispatch.hosted_agent, "invoke_agent",
                        lambda sdk, state: seen.update(state) or {"output": "ok"})
    monkeypatch.setattr(genai, "resolve_project_ocid", lambda *a, **kw: "ocid1.project.oc1..auto")

    _run_dispatch({"framework": "langgraph"})
    assert seen["project_ocid"] == "ocid1.project.oc1..auto"


def test_dispatch_surfaces_project_resolution_error(monkeypatch, deployed):
    """FIX-47 と同じ方針: 空の OpenAi-Project で invoke せず、復旧手順を含む理由を返す。"""
    from jetuse_core import genai
    from service import agent_dispatch

    def boom(*a, **kw):
        raise genai.ProjectResolutionError("PROJECT_OCID を設定してください")

    monkeypatch.setattr(genai, "resolve_project_ocid", boom)
    monkeypatch.setattr(agent_dispatch.hosted_agent, "invoke_agent",
                        lambda sdk, state: pytest.fail("must not invoke without a project"))

    body = _run_dispatch({"framework": "adk"})
    assert "PROJECT_OCID を設定してください" in body
    assert "[DONE]" in body


def test_dispatch_reports_undeployed_sdk_without_side_effects(monkeypatch):
    """未配備なら RAG store 参照や project 自動作成に触れる前に理由を返す。
    副作用のあとで落ちると、本当の理由(認証無効/対象外リージョン/SDK未配備)が別のエラーに隠れる。"""
    from jetuse_core import genai, rag
    from service import agent_dispatch

    monkeypatch.setattr(rag, "get_store_id", lambda *a, **kw: pytest.fail("must not touch RAG"))
    monkeypatch.setattr(genai, "resolve_project_ocid",
                        lambda *a, **kw: pytest.fail("must not resolve/create a project"))
    monkeypatch.setattr(agent_dispatch.hosted_agent, "invoke_agent",
                        lambda sdk, state: pytest.fail("must not invoke"))

    body = _run_dispatch({"framework": "openai_agents", "enabled_tools": ["rag_search"]})
    assert "配備されていません" in body
    assert "[DONE]" in body
