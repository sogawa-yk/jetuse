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
